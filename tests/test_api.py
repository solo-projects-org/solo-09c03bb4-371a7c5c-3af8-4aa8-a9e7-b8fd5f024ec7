"""端到端 API 测试：底表固定、批次整批替换、失败回滚、并发重传、重启持久化。"""

from __future__ import annotations

import importlib
import threading

import pytest
from fastapi.testclient import TestClient

BASE_CSV = "group,count\n神经,5\n代谢,4\n免疫,3\n".encode("utf-8")


def _csv(rows):
    head = "patient_id,group\n"
    return (head + "".join(f"{pid},{grp}\n" for pid, grp in rows)).encode("utf-8")


# 与样例一致的批次数据。
A_FIRST = _csv([
    ("P0001", "神经"), ("P0002", "神经"), ("P0002", "神经"),
    ("P0003", "代谢"), ("P0004", "代谢"),
    ("P0005", "免疫"),
])
B_FIRST = _csv([
    ("P0001", "神经"),
    ("P0006", "神经"), ("P0007", "神经"), ("P0008", "神经"),
    ("P0003", "免疫"),
    ("P0009", "代谢"), ("P0010", "代谢"),
    ("P0011", "免疫"), ("P0012", "免疫"),
])
B_CORRECTED = B_FIRST.replace("P0003,免疫".encode(), "P0003,代谢".encode())


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个测试使用独立 SQLite 文件，并重新加载应用以重置连接。"""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("APP_DB_PATH", str(db_path))
    import app.main as main_module
    importlib.reload(main_module)
    with TestClient(main_module.app) as c:
        yield c, main_module, db_path


def _upload_base(c, raw=BASE_CSV, status=200):
    res = c.post("/api/base", files={"file": ("base.csv", raw, "text/csv")})
    assert res.status_code == status, res.text
    return res


def _upload_batch(c, name, raw, status=200):
    res = c.post(
        "/api/batches",
        data={"batch_name": name},
        files={"file": ("batch.csv", raw, "text/csv")},
    )
    assert res.status_code == status, res.text
    return res


def _groups(report):
    return {g["group"]: g for g in report["groups"]}


# ---------------------------------------------------------------- 底表

def test_state_starts_uninitialized(client):
    c, _, _ = client
    state = c.get("/api/state").json()
    assert state["initialized"] is False
    assert state["base"] is None
    assert state["batches"] == []
    assert state["report"]["passed"] is False


def test_base_upload_is_fixed_and_not_overwritten(client):
    c, _, _ = client
    state = _upload_base(c).json()
    assert state["initialized"] is True
    assert state["base"]["total"] == 12
    assert [g["name"] for g in state["base"]["groups"]] == ["神经", "代谢", "免疫"]
    _upload_base(c, "group,count\n甲,1\n乙,2\n".encode("utf-8"), status=409)
    assert c.get("/api/state").json()["base"]["total"] == 12


def test_batch_before_base_rejected(client):
    c, _, _ = client
    _upload_batch(c, "a", A_FIRST, status=409)


def test_bad_base_csv_keeps_no_state(client):
    c, _, _ = client
    _upload_base(c, b"group,count\nA,99999\nB,1\n", status=400)
    assert c.get("/api/state").json()["initialized"] is False


# ---------------------------------------------------------------- 批次替换

def test_batches_reconcile_across_batches(client):
    c, _, _ = client
    _upload_base(c)
    _upload_batch(c, "batch-a", A_FIRST)
    state = c.get("/api/state").json()
    assert [b["name"] for b in state["batches"]] == ["batch-a"]
    assert state["report"]["passed"] is False
    g = _groups(state["report"])
    assert (g["神经"]["dedup_count"], g["代谢"]["dedup_count"], g["免疫"]["dedup_count"]) == (2, 2, 1)

    _upload_batch(c, "batch-b", B_FIRST)
    report = c.get("/api/state").json()["report"]
    assert report["conflict_count"] == 1
    assert report["conflicts"][0]["patient_id"] == "P0003"
    assert _groups(report)["代谢"]["dedup_count"] == 3
    assert report["passed"] is False

    # 重传纠正后的 batch-b：整批替换，冲突消失且对账通过。
    _upload_batch(c, "batch-b", B_CORRECTED)
    report = c.get("/api/state").json()["report"]
    assert report["conflict_count"] == 0
    assert report["passed"] is True
    assert all(g["difference"] == 0 for g in report["groups"])
    batch_b = next(b for b in c.get("/api/state").json()["batches"] if b["name"] == "batch-b")
    assert batch_b["revision"] == 2


def test_reupload_replaces_whole_batch_not_merge(client):
    c, _, _ = client
    _upload_base(c)
    _upload_batch(c, "batch-a", A_FIRST)
    # 重传一个只含一行的版本：旧批次 6 行全部消失，而不是合并。
    _upload_batch(c, "batch-a", _csv([("P0001", "神经")]))
    state = c.get("/api/state").json()
    assert state["batches"][0]["row_count"] == 1
    assert state["report"]["total_rows"] == 1


def test_unknown_group_in_batch_rejected_and_old_batch_survives(client):
    c, _, _ = client
    _upload_base(c)
    _upload_batch(c, "batch-a", A_FIRST)
    before = c.get("/api/state").json()
    _upload_batch(c, "batch-a", _csv([("P1", "未知组")]), status=400)
    after = c.get("/api/state").json()
    assert after["batches"] == before["batches"]
    assert after["report"] == before["report"]


def test_parse_failure_does_not_touch_batches(client):
    c, _, _ = client
    _upload_base(c)
    _upload_batch(c, "batch-a", A_FIRST)
    _upload_batch(c, "batch-a", b"group,count\nX,1\n", status=400)  # 缺 patient_id 列
    _upload_batch(c, "batch-a", b"", status=400)
    state = c.get("/api/state").json()
    assert state["batches"][0]["row_count"] == 6
    assert state["report"]["passed"] is False
    assert _groups(state["report"])["神经"]["dedup_count"] == 2


def test_storage_failure_rolls_back_and_old_batch_survives(client, monkeypatch):
    c, main_module, _ = client
    _upload_base(c)
    _upload_batch(c, "batch-a", A_FIRST)
    before = c.get("/api/state").json()

    def boom(name, rows):
        raise main_module.DatabaseError("模拟磁盘故障")

    monkeypatch.setattr(main_module.db, "replace_batch", boom)
    res = c.post(
        "/api/batches",
        data={"batch_name": "batch-a"},
        files={"file": ("batch.csv", B_FIRST, "text/csv")},
    )
    assert res.status_code == 500
    after = c.get("/api/state").json()
    assert after["batches"] == before["batches"]
    assert after["report"] == before["report"]


def test_batch_name_validation(client):
    c, _, _ = client
    _upload_base(c)
    for bad in ("", "   ", "a/b", "a b\n"):
        res = c.post(
            "/api/batches",
            data={"batch_name": bad},
            files={"file": ("batch.csv", A_FIRST, "text/csv")},
        )
        assert res.status_code == 400
    res = c.post(
        "/api/batches",
        data={"batch_name": "x" * 65},
        files={"file": ("batch.csv", A_FIRST, "text/csv")},
    )
    assert res.status_code == 400
    assert c.get("/api/state").json()["batches"] == []


# ---------------------------------------------------------------- 并发

def _concurrent(n, fn):
    barrier = threading.Barrier(n)
    results = []

    def worker(i):
        barrier.wait()
        results.append(fn(i))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_reupload_same_batch_yields_only_whole_versions(client):
    """多个并发重传同一批次：任何读取只能看到某一完整版本的行数/指纹。"""
    c, _, _ = client
    _upload_base(c)
    # 每个版本的行数与分组人数组合都不同，混合版本会被断言捕获。
    variants = {
        1: _csv([("P0001", "神经")]),
        2: _csv([("P0002", "代谢"), ("P0003", "免疫")]),
        3: _csv([("P0004", "神经"), ("P0005", "神经"), ("P0006", "免疫")]),
    }
    seen_counts = []
    errors = []
    stop = threading.Event()

    def uploader(i):
        raw = variants[(i % len(variants)) + 1]
        return c.post(
            "/api/batches",
            data={"batch_name": "batch-a"},
            files={"file": ("batch.csv", raw, "text/csv")},
        ).status_code

    def reader():
        while not stop.is_set():
            try:
                state = c.get("/api/state").json()
                matches = [b for b in state["batches"] if b["name"] == "batch-a"]
                if not matches:
                    # 批次尚不存在（并发写入尚未提交）——同样是一致的状态。
                    assert state["report"]["total_rows"] == 0
                    continue
                batch = matches[0]
                assert batch["row_count"] in variants
                assert state["report"]["total_rows"] == batch["row_count"]
                seen_counts.append(batch["row_count"])
            except Exception as exc:  # noqa: BLE001 - 收集后统一断言
                errors.append(exc)

    readers = [threading.Thread(target=reader) for _ in range(3)]
    for t in readers:
        t.start()
    statuses = _concurrent(6, uploader)
    stop.set()
    for t in readers:
        t.join()

    assert errors == []
    assert all(code == 200 for code in statuses)
    assert seen_counts  # 读线程确实观察到了中间状态

    state = c.get("/api/state").json()
    batch = next(b for b in state["batches"] if b["name"] == "batch-a")
    assert batch["row_count"] in variants
    assert state["report"]["total_rows"] == batch["row_count"]
    counts = {g["group"]: g["dedup_count"] for g in state["report"]["groups"]}
    # 各版本对应的分组人数组合互不相同，最终必须恰好是其中之一。
    assert (counts["神经"], counts["代谢"], counts["免疫"]) in {(1, 0, 0), (0, 1, 1), (2, 0, 1)}
    # 批次版本号单调递增，最终 revision 等于上传成功次数。
    assert batch["revision"] == statuses.count(200)


def test_concurrent_reupload_never_mixes_rows(client):
    """并发替换后，最终内容必须恰好是某一版本，而不是多版本混合。"""
    c, _, _ = client
    _upload_base(c)
    v1 = [("P0001", "神经")]
    v2 = [("P0002", "代谢"), ("P0003", "免疫")]

    def uploader(i):
        rows = v1 if i % 2 == 0 else v2
        return c.post(
            "/api/batches",
            data={"batch_name": "batch-a"},
            files={"file": ("batch.csv", _csv(rows), "text/csv")},
        ).status_code

    assert all(code == 200 for code in _concurrent(8, uploader))

    report = c.get("/api/state").json()
    counts = {g["group"]: g["dedup_count"] for g in report["report"]["groups"]}
    # v1: 神经1 代谢0 免疫0；v2: 神经0 代谢1 免疫1。混合会出现 (1,1,1) 等不可能组合。
    assert (counts["神经"], counts["代谢"], counts["免疫"]) in {(1, 0, 0), (0, 1, 1)}
    assert report["batches"][0]["row_count"] in (1, 2)


# ---------------------------------------------------------------- 重启

def test_base_and_batches_persist_across_restart(client, monkeypatch):
    c, main_module, _ = client
    _upload_base(c)
    _upload_batch(c, "batch-a", A_FIRST)
    _upload_batch(c, "batch-b", B_FIRST)

    importlib.reload(main_module)
    new_client = TestClient(main_module.app)
    state = new_client.get("/api/state").json()
    assert state["initialized"] is True
    assert state["base"]["total"] == 12
    assert [b["name"] for b in state["batches"]] == ["batch-a", "batch-b"]
    assert state["report"]["conflict_count"] == 1

    # 重启后底表仍不可被替换，批次仍可整批替换并重新对账。
    res = new_client.post(
        "/api/base", files={"file": ("base.csv", BASE_CSV, "text/csv")}
    )
    assert res.status_code == 409
    res = new_client.post(
        "/api/batches",
        data={"batch_name": "batch-b"},
        files={"file": ("batch.csv", B_CORRECTED, "text/csv")},
    )
    assert res.status_code == 200
    assert new_client.get("/api/state").json()["report"]["passed"] is True


def test_no_temp_journal_left_over_written_batches(client, tmp_path):
    """写入后数据库可再次打开，说明没有未提交事务或残留日志影响读取。"""
    c, main_module, db_path = client
    _upload_base(c)
    _upload_batch(c, "batch-a", A_FIRST)
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM base_groups").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM batch_rows").fetchone()[0] == 6
    finally:
        conn.close()
