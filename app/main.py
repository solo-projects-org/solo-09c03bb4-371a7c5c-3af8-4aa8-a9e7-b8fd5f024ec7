"""罕见病分批补报对账服务。

数据模型
--------
* **底表**：首次上传后永久固定的组名与人数；之后任何路径都不会修改它，
  因此底表与底表合计（已公布合计）在重启、换批次、上传失败后都不变。
* **批次明细**：网页按批次上传含 ``patient_id, group`` 的 CSV；
  再次上传同名批次即**整批替换**该批次明细，组名必须是底表中的组。
* **对账报告**：按当前所有批次的 ``patient_id`` 去重——同组重复只计一人，
  跨组出现的 ID 列为冲突且不计入任何组；逐组给出去重人数、底表人数、差值；
  只有无冲突且各组差值均为 0 才显示对账通过。

一致性
------
* 批次替换是单个 SQLite 写事务；报告读取是单个读事务快照。
  两个 Web 进程并发重传同一批次时，任何一次读取只能看到某个完整版本。
* 解析失败（400）或保存失败（500）不会触碰旧批次，旧批次与对账结果继续可用。
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .csv_utils import (
    CSVValidationError,
    parse_base_csv,
    parse_batch_csv,
)
from .database import Database, DatabaseError
from .reconcile import reconcile

DB_PATH = os.environ.get(
    "APP_DB_PATH",
    str(Path(__file__).resolve().parent.parent / "data" / "app.db"),
)
STATIC_DIR = Path(__file__).resolve().parent / "static"
SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"

MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MiB，超过即拒绝，避免撑爆内存
MAX_BATCH_NAME = 64
# 批次名不允许控制字符、首尾空白；允许中英文、数字、下划线、点、横线。
_BATCH_NAME_RE = re.compile(r"^[\w.\-]+$", re.UNICODE)

app = FastAPI(title="罕见病分批补报对账", version="2.0.0")
db = Database(DB_PATH)
# 演示用 CSV 只读发布，网页可直接载入样例文件。
app.mount("/samples", StaticFiles(directory=SAMPLES_DIR), name="samples")


# ---------------------------------------------------------------- 辅助

def _read_upload(file: UploadFile) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = file.file.read(64 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"CSV 超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB 上限",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_batch_name(raw: str | None) -> str:
    name = (raw or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="请填写批次名")
    if len(name) > MAX_BATCH_NAME:
        raise HTTPException(
            status_code=400, detail=f"批次名最多 {MAX_BATCH_NAME} 个字符"
        )
    if not _BATCH_NAME_RE.fullmatch(name):
        raise HTTPException(
            status_code=400,
            detail="批次名只能包含中英文、数字、下划线、点或横线",
        )
    return name


def _payload(snapshot: dict) -> dict:
    """把一个数据库快照转换为对账报告 + 状态。纯函数，保证两者同源。"""
    base: list[tuple[str, int]] = snapshot["base"]
    report = reconcile(base, snapshot["rows"]).as_dict()
    report["base_total"] = sum(size for _, size in base)
    initialized = bool(base)
    return {
        "initialized": initialized,
        "base": (
            {
                "groups": [
                    {"name": name, "count": count} for name, count in base
                ],
                "total": sum(size for _, size in base),
            }
            if initialized
            else None
        ),
        "batches": snapshot["batches"],
        "report": report,
    }


def _current_payload() -> dict:
    return _payload(db.snapshot())


# ---------------------------------------------------------------- 路由

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
def get_state() -> dict:
    """底表、批次列表与对账报告，全部来自同一个已提交快照。"""
    return _current_payload()


@app.post("/api/base")
def upload_base(file: UploadFile = File(...)) -> dict:
    """首次上传并固定底表；底表已存在时返回 409，绝不覆盖。"""
    raw = _read_upload(file)
    try:
        groups = parse_base_csv(raw)
    except CSVValidationError as exc:
        raise HTTPException(status_code=400, detail=f"底表 CSV 校验失败：{exc}") from exc

    try:
        db.insert_base_table(groups)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (DatabaseError, sqlite3.Error) as exc:
        raise HTTPException(
            status_code=500, detail=f"底表保存失败，未做任何修改：{exc}"
        ) from exc

    payload = _current_payload()
    payload["message"] = "底表已固定"
    return payload


@app.post("/api/batches")
def upload_batch(
    file: UploadFile = File(...),
    batch_name: str | None = Form(None),
) -> dict:
    """上传（或整批替换）一个批次。

    * 解析/校验失败 → 400，旧批次与对账结果保持可用；
    * 底表尚未上传 → 409；
    * 保存失败 → 500，事务回滚，旧批次保持可用。
    """
    name = _validate_batch_name(batch_name)
    raw = _read_upload(file)

    snapshot = db.snapshot()
    base: list[tuple[str, int]] = snapshot["base"]
    if not base:
        raise HTTPException(status_code=409, detail="底表尚未上传，无法上传批次")

    allowed = {group for group, _ in base}
    try:
        rows = parse_batch_csv(raw, allowed)
    except CSVValidationError as exc:
        raise HTTPException(status_code=400, detail=f"批次 CSV 校验失败：{exc}") from exc

    try:
        db.replace_batch(name, [(row.patient_id, row.group) for row in rows])
    except (DatabaseError, sqlite3.Error) as exc:
        raise HTTPException(
            status_code=500, detail=f"批次保存失败，旧批次未受影响：{exc}"
        ) from exc

    payload = _current_payload()
    uploaded = next(
        (b for b in payload["batches"] if b["name"] == name), None
    )
    payload["uploaded_batch"] = uploaded
    return payload


@app.get("/api/report")
def get_report() -> dict:
    """仅返回对账报告（与 /api/state 使用同一快照构造方式）。"""
    return _payload(db.snapshot())["report"]
