"""对账去重规则测试，以及 CSV 校验测试。"""

from __future__ import annotations

import pytest

from app.csv_utils import CSVValidationError, parse_base_csv, parse_batch_csv
from app.reconcile import reconcile

BASE = [("神经", 5), ("代谢", 4), ("免疫", 3)]


def _report(batches):
    return reconcile(BASE, batches).as_dict()


# ---------------------------------------------------------------- 去重


def test_same_id_same_group_counted_once():
    # P1 在两个批次、同一组 → 只计一人。
    r = _report({
        "a": [("P1", "神经"), ("P2", "神经")],
        "b": [("P1", "神经")],
    })
    assert r["total_rows"] == 3
    assert r["total_patients"] == 2
    assert r["duplicate_rows"] == 1
    assert r["conflicts"] == []
    neuro = next(g for g in r["groups"] if g["group"] == "神经")
    assert neuro["dedup_count"] == 2
    assert neuro["base_count"] == 5
    assert neuro["difference"] == -3


def test_same_id_same_group_within_one_batch_counted_once():
    # 同一批次内同一 patient_id 重复（同组）按一次上传的去重规则只计一人。
    r = _report({"a": [("P1", "代谢"), ("P1", "代谢"), ("P1", "代谢")]})
    assert r["total_patients"] == 1
    assert r["duplicate_rows"] == 2
    assert r["conflicts"] == []


def test_cross_group_id_is_conflict_and_counted_in_no_group():
    r = _report({
        "a": [("P9", "神经")],
        "b": [("P9", "代谢")],
    })
    assert r["conflict_count"] == 1
    assert r["conflicts"] == [{"patient_id": "P9", "groups": ["代谢", "神经"]}]
    counts = {g["group"]: g["dedup_count"] for g in r["groups"]}
    assert counts == {"神经": 0, "代谢": 0, "免疫": 0}
    assert r["passed"] is False


def test_conflict_excluded_even_when_other_rows_balance():
    # 若把冲突 ID 悄悄计入某一组，合计会凑巧平衡；规则要求它不计入任何组。
    r = _report({
        "a": [("P1", "神经"), ("P1", "神经")],
        "b": [("P1", "代谢")],
    })
    counts = {g["group"]: g["dedup_count"] for g in r["groups"]}
    assert counts == {"神经": 0, "代谢": 0, "免疫": 0}
    assert r["passed"] is False


def test_report_passes_only_when_no_conflicts_and_all_groups_equal():
    aligned = {
        "a": [
            ("N1", "神经"), ("N2", "神经"), ("N3", "神经"), ("N4", "神经"), ("N5", "神经"),
            ("M1", "代谢"), ("M2", "代谢"), ("M3", "代谢"), ("M4", "代谢"),
            ("I1", "免疫"), ("I2", "免疫"), ("I3", "免疫"),
        ]
    }
    r = _report(aligned)
    assert r["passed"] is True
    assert r["conflict_count"] == 0
    assert all(g["difference"] == 0 for g in r["groups"])
    assert sum(g["dedup_count"] for g in r["groups"]) == sum(c for _, c in BASE)


def test_equal_counts_but_conflict_still_fails():
    aligned = {
        "a": [
            ("N1", "神经"), ("N2", "神经"), ("N3", "神经"), ("N4", "神经"),
            ("N5", "神经"),
            ("M1", "代谢"), ("M2", "代谢"), ("M3", "代谢"), ("M4", "代谢"),
            ("I1", "免疫"), ("I2", "免疫"), ("I3", "免疫"),
        ]
    }
    assert _report(aligned)["passed"] is True

    # 冲突 ID：N5 在一个批次记为神经、另一个批次记为代谢。
    # 它不计入任何组，神经因此少 1 人，即使“总行数看起来够”也不能通过。
    conflicted = {
        "a": aligned["a"] + [("N5", "神经")],
        "b": [("N5", "代谢")],
    }
    r = _report(conflicted)
    assert r["conflict_count"] == 1
    counts = {g["group"]: g["dedup_count"] for g in r["groups"]}
    assert counts == {"神经": 4, "代谢": 4, "免疫": 3}
    assert r["passed"] is False


def test_empty_batches_report_not_passed():
    r = _report({})
    assert r["total_rows"] == 0
    assert r["passed"] is False
    assert [g["dedup_count"] for g in r["groups"]] == [0, 0, 0]


def test_difference_sign_and_group_order():
    r = _report({"a": [("X1", "神经"), ("X2", "神经")]})
    assert [g["group"] for g in r["groups"]] == ["神经", "代谢", "免疫"]
    neuro = r["groups"][0]
    assert neuro["difference"] == 2 - 5


# ---------------------------------------------------------------- CSV

BASE_CSV = "group,count\n神经,5\n代谢,4\n免疫,3\n"


def test_parse_base_csv_accepts_header_and_orders():
    groups = parse_base_csv(BASE_CSV)
    assert groups == [("神经", 5), ("代谢", 4), ("免疫", 3)]


@pytest.mark.parametrize("bad", [
    b"group,count\nA,99999\nB,1\n",
    b"group,count\nA,2\nA,3\n",
    b"group,count\nA,2\n",
    b"name\nA\n",
    b"group,count\nA,2.5\nB,1\n",
])
def test_parse_base_csv_rejects_invalid(bad):
    with pytest.raises(CSVValidationError):
        parse_base_csv(bad)


BATCH_CSV = "patient_id,group\nP1,神经\nP2,代谢\n".encode("utf-8")


def test_parse_batch_csv_accepts():
    allowed = {"神经", "代谢", "免疫"}
    rows = parse_batch_csv(BATCH_CSV, allowed)
    assert [(r.patient_id, r.group) for r in rows] == [("P1", "神经"), ("P2", "代谢")]


def test_parse_batch_csv_unknown_group_rejected():
    with pytest.raises(CSVValidationError):
        parse_batch_csv("patient_id,group\nP1,未知组\n".encode("utf-8"), {"神经"})


@pytest.mark.parametrize("bad", [
"patient_id,group\n,神经\n".encode("utf-8"),
"patient_id,group\nP1,\n".encode("utf-8"),
    b"patient_id\nP1\n",
"patient_id,group\nP1\n".encode("utf-8"),
])
def test_parse_batch_csv_rejects_bad_rows(bad):
    with pytest.raises(CSVValidationError):
        parse_batch_csv(bad, {"神经"})


def test_parse_batch_csv_duplicate_ids_within_batch_allowed():
    # 批次内重复不报错：由对账规则统一处理（同组只计一人，跨组冲突）。
    rows = parse_batch_csv(
        "patient_id,group\nP1,神经\nP1,代谢\n".encode("utf-8"), {"神经", "代谢"}
    )
    assert len(rows) == 2
