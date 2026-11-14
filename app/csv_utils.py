"""CSV 解析与校验：固定底表、批次病例明细。

底表 CSV（首次上传后永久固定）::

    group,count
    神经,5
    代谢,4

要求：2~16 个唯一组名，人数为 0~1000 的整数。

批次明细 CSV（可反复上传，同名批次整批替换）::

    patient_id,group
    P0001,神经
    P0002,代谢

要求：表头至少包含 ``patient_id`` 与 ``group``，最多 5000 行；
patient_id、group 去空格后非空；组名必须是固定底表中已有的组。
同一 patient_id 在批次内重复不报错——对账阶段按“同组只计一人、
跨组冲突”统一处理。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

MIN_BASE_GROUPS = 2
MAX_BASE_GROUPS = 16
MIN_BASE_COUNT = 0
MAX_BASE_COUNT = 1000

MAX_BATCH_ROWS = 5000
MAX_ID_LENGTH = 128
MAX_GROUP_LENGTH = 128


class CSVValidationError(ValueError):
    """CSV 内容不符合要求。"""


def _decode(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise CSVValidationError("CSV 必须为 UTF-8 编码") from exc
    return raw


def _non_empty_rows(text: str) -> list[list[str]]:
    reader = csv.reader(io.StringIO(text))
    return [row for row in reader if any(cell.strip() for cell in row)]


def _header_index(rows: list[list[str]], candidates: tuple[str, ...]) -> int:
    header = [cell.strip().lower() for cell in rows[0]]
    for cand in candidates:
        if cand in header:
            return header.index(cand)
    raise CSVValidationError(f"表头缺少列 {candidates}，实际表头：{rows[0]!r}")


# ---------------------------------------------------------------- 底表


def parse_base_csv(raw: bytes | str) -> list[tuple[str, int]]:
    """解析并校验底表 CSV，返回按文件顺序排列的 ``[(组名, 人数), ...]``。"""
    rows = _non_empty_rows(_decode(raw))
    if len(rows) < 2:
        raise CSVValidationError("CSV 至少需要表头行和一行数据")

    name_col = _header_index(rows, ("group", "group_name", "name", "组名", "名称"))
    count_col = _header_index(rows, ("count", "size", "cases", "人数", "病例数"))
    if name_col == count_col:
        raise CSVValidationError("组名列与人数列不能是同一列")

    groups: list[tuple[str, int]] = []
    seen: set[str] = set()
    for line_no, row in enumerate(rows[1:], start=2):
        if len(row) <= max(name_col, count_col):
            raise CSVValidationError(f"第 {line_no} 行列数不足：{row!r}")
        name = row[name_col].strip()
        count_raw = row[count_col].strip()
        if not name:
            raise CSVValidationError(f"第 {line_no} 行组名为空")
        if len(name) > MAX_GROUP_LENGTH:
            raise CSVValidationError(f"第 {line_no} 行组名超过 {MAX_GROUP_LENGTH} 个字符")
        if name in seen:
            raise CSVValidationError(f"组名重复：{name}")
        try:
            count = int(count_raw)
        except ValueError as exc:
            raise CSVValidationError(f"第 {line_no} 行人数不是整数：{count_raw!r}") from exc
        if not (MIN_BASE_COUNT <= count <= MAX_BASE_COUNT):
            raise CSVValidationError(
                f"第 {line_no} 行人数 {count} 超出允许范围 "
                f"[{MIN_BASE_COUNT}, {MAX_BASE_COUNT}]：组 {name}"
            )
        seen.add(name)
        groups.append((name, count))

    if not (MIN_BASE_GROUPS <= len(groups) <= MAX_BASE_GROUPS):
        raise CSVValidationError(
            f"唯一组名数量需在 {MIN_BASE_GROUPS}~{MAX_BASE_GROUPS} 之间，当前为 {len(groups)}"
        )
    return groups


# ---------------------------------------------------------------- 批次


@dataclass(frozen=True)
class BatchRow:
    patient_id: str
    group: str


def parse_batch_csv(raw: bytes | str, allowed_groups: set[str]) -> list[BatchRow]:
    """解析并校验批次明细 CSV；任一行不合法则整批拒绝。

    ``allowed_groups`` 为固定底表中的组名集合，出现未知组名即拒绝。
    """
    rows = _non_empty_rows(_decode(raw))
    if len(rows) < 2:
        raise CSVValidationError("CSV 至少需要表头行和一行数据")

    id_col = _header_index(
        rows, ("patient_id", "patientid", "patient", "病例编号", "患者编号")
    )
    group_col = _header_index(rows, ("group", "group_name", "组名"))
    if id_col == group_col:
        raise CSVValidationError("patient_id 与 group 不能是同一列")

    if len(rows) - 1 > MAX_BATCH_ROWS:
        raise CSVValidationError(
            f"单批次最多 {MAX_BATCH_ROWS} 行数据，当前为 {len(rows) - 1} 行"
        )

    parsed: list[BatchRow] = []
    for line_no, row in enumerate(rows[1:], start=2):
        if len(row) <= max(id_col, group_col):
            raise CSVValidationError(f"第 {line_no} 行列数不足：{row!r}")
        patient_id = row[id_col].strip()
        group = row[group_col].strip()
        if not patient_id:
            raise CSVValidationError(f"第 {line_no} 行 patient_id 为空")
        if len(patient_id) > MAX_ID_LENGTH:
            raise CSVValidationError(
                f"第 {line_no} 行 patient_id 超过 {MAX_ID_LENGTH} 个字符"
            )
        if not group:
            raise CSVValidationError(f"第 {line_no} 行 group 为空")
        if len(group) > MAX_GROUP_LENGTH:
            raise CSVValidationError(
                f"第 {line_no} 行组名超过 {MAX_GROUP_LENGTH} 个字符"
            )
        if group not in allowed_groups:
            raise CSVValidationError(f"第 {line_no} 行组名 {group!r} 不在底表中")
        parsed.append(BatchRow(patient_id=patient_id, group=group))

    return parsed
