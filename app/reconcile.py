"""对账：按当前全部批次的 patient_id 去重，逐组与固定底表比较人数。

规则
----
* 只按 ``patient_id`` 去重：同一 ID 出现在多个批次、且组名相同 → 只计一人；
* 同一 ID 出现在不同组 → 列为**冲突**，不计入任何组；
* 逐组输出：去重人数、底表人数、差值（去重人数 − 底表人数）；
* 只有**没有冲突**且**每个组的差值都为 0** 时，对账才通过；
* 底表取自同一个数据库快照，批次整批替换只会改变批次明细，
  不会改变底表与已公布合计。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GroupReconciliation:
    group: str
    dedup_count: int
    base_count: int
    difference: int

    def as_dict(self) -> dict:
        return {
            "group": self.group,
            "dedup_count": self.dedup_count,
            "base_count": self.base_count,
            "difference": self.difference,
            "matched": self.difference == 0,
        }


@dataclass
class ReconciliationReport:
    batches: list[str] = field(default_factory=list)
    total_patients: int = 0
    total_rows: int = 0
    duplicate_rows: int = 0
    conflicts: list[dict] = field(default_factory=list)
    groups: list[GroupReconciliation] = field(default_factory=list)
    passed: bool = False

    def as_dict(self) -> dict:
        return {
            "batches": self.batches,
            "total_patients": self.total_patients,
            "total_rows": self.total_rows,
            "duplicate_rows": self.duplicate_rows,
            "conflict_count": len(self.conflicts),
            "conflicts": self.conflicts,
            "groups": [g.as_dict() for g in self.groups],
            "passed": self.passed,
        }


def reconcile(
    base_groups: list[tuple[str, int]],
    batch_rows: dict[str, list[tuple[str, str]]],
) -> ReconciliationReport:
    """对当前全部批次做去重对账。

    参数
    ----
    base_groups:
        固定底表 ``[(组名, 底表人数), ...]``，顺序即输出顺序。
    batch_rows:
        ``{批次名: [(patient_id, group), ...]}``，来自同一个数据库快照。
    """
    group_order = [name for name, _ in base_groups]
    base_count = dict(base_groups)

    # patient_id -> 见到的组名集合（去重）。
    seen_groups: dict[str, set[str]] = {}
    total_rows = 0

    for batch_name in sorted(batch_rows):
        for patient_id, group in batch_rows[batch_name]:
            total_rows += 1
            # 未知组名在解析阶段已被拒绝；这里仍统一记入组名集合，
            # 兜底保证这类 ID 不可能被静默计入某个组。
            seen_groups.setdefault(patient_id, set()).add(group)

    # 一个 ID 只对应一个组：确认计入；对应多个组：冲突，不计入任何组。
    confirmed: dict[str, str] = {}
    conflicts: list[dict] = []
    for patient_id, groups in seen_groups.items():
        if len(groups) == 1:
            confirmed[patient_id] = next(iter(groups))
        else:
            conflicts.append(
                {"patient_id": patient_id, "groups": sorted(groups)}
            )

    conflicts.sort(key=lambda item: item["patient_id"])

    counts = {name: 0 for name in group_order}
    for group in confirmed.values():
        counts[group] += 1

    groups_out = [
        GroupReconciliation(
            group=name,
            dedup_count=counts[name],
            base_count=base_count[name],
            difference=counts[name] - base_count[name],
        )
        for name in group_order
    ]

    passed = (
        bool(group_order)
        and total_rows > 0
        and not conflicts
        and all(g.difference == 0 for g in groups_out)
    )

    return ReconciliationReport(
        batches=sorted(batch_rows),
        total_patients=len(seen_groups),
        total_rows=total_rows,
        duplicate_rows=total_rows - len(seen_groups),
        conflicts=conflicts,
        groups=groups_out,
        passed=passed,
    )
