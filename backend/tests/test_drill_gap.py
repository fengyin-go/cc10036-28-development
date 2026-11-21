"""城市下钻空层（缺层）端到端测试。

覆盖需求：
1. 两类空层：缺维属性（杭州，已收订单但缺门店值）与缺事实分区（武汉，分区未构建）；
2. 缺层数据保留父行已核算收入与空层原因，并可关联立方分区读详情；
3. 立方构建失败后重试，相邻父级分区不得带入当前门店层；
4. 已算金额与待归类收入全程可对账（parent == classified + pending）；
5. 分别在缺维属性、缺事实分区发起下钻并完成回补，核对补齐后的可进入项。
"""
from __future__ import annotations

import pytest

from app.analytics_engine import (
    GAP_STATUS_CLASSIFIED,
    GAP_STATUS_FAILED,
    MISSING_DIMENSION_ATTR,
    MISSING_FACT_PARTITION,
    PARTITION_FAILED,
    PARTITION_NOT_BUILT,
    PARTITION_ONLINE,
    CubePartition,
    build_drill_gap_record,
)
from app.services.drill_gap_registry import (
    CUBE_NAME,
    DEMO_FACT_ROWS,
    DEMO_PARENT_FACTS,
    LEVEL_DIMENSION,
    MEASURE_NAMES,
    PARENT_DIMENSION,
    DrillGapRegistry,
)


@pytest.fixture()
def registry() -> DrillGapRegistry:
    reg = DrillGapRegistry()
    reg.reset()
    return reg


def _gap_ids(registry: DrillGapRegistry, city: str) -> str:
    return next(g["gap_id"] for g in registry.list_gaps() if g["parent_value"] == city)


# ---------------------------------------------------------------------------
# 空层解释与父行数据保留
# ---------------------------------------------------------------------------

def test_two_distinct_empty_layer_reasons(registry: DrillGapRegistry) -> None:
    gaps = {g["parent_value"]: g for g in registry.list_gaps()}
    assert gaps["杭州"]["reason_code"] == MISSING_DIMENSION_ATTR
    assert gaps["武汉"]["reason_code"] == MISSING_FACT_PARTITION
    # 同一种“空层”在两座城市的不同成因都给出可读解释
    explanations = {e["reason_code"]: e for e in registry.empty_layer_explanations()}
    assert set(explanations) == {MISSING_DIMENSION_ATTR, MISSING_FACT_PARTITION}
    assert explanations[MISSING_DIMENSION_ATTR]["affected_parents"] == ["杭州"]
    assert explanations[MISSING_FACT_PARTITION]["affected_parents"] == ["武汉"]


def test_parent_revenue_and_summary_preserved(registry: DrillGapRegistry) -> None:
    hz = registry.get_gap(_gap_ids(registry, "杭州"))
    wh = registry.get_gap(_gap_ids(registry, "武汉"))
    # 父行已经核算的收入原样保留
    assert hz.parent_measures == {"revenue": 10000.0, "order_count": 100.0}
    assert wh.parent_measures == {"revenue": 8000.0, "order_count": 80.0}
    assert hz.parent_revenue == 10000.0 and wh.parent_revenue == 8000.0


def test_initial_classified_and_pending_split(registry: DrillGapRegistry) -> None:
    hz = registry.get_gap(_gap_ids(registry, "杭州"))
    wh = registry.get_gap(_gap_ids(registry, "武汉"))
    # 杭州：7000 已有门店归位，3000 因缺门店维值待归类
    assert hz.classified_measures["revenue"] == 7000.0
    assert hz.pending_measures["revenue"] == 3000.0
    # 武汉：门店层分区未构建，门店层已归位为 0，全部 8000 待回补
    assert wh.classified_measures["revenue"] == 0.0
    assert wh.pending_measures["revenue"] == 8000.0
    assert hz.is_balanced() and wh.is_balanced()


# ---------------------------------------------------------------------------
# 下钻详情页：缺层数据关联立方分区
# ---------------------------------------------------------------------------

def test_detail_links_partition_by_parent_key(registry: DrillGapRegistry) -> None:
    wh_id = _gap_ids(registry, "武汉")
    detail = registry.gap_detail(wh_id)
    assert detail["ok"] is True
    assert detail["partition"]["partition_id"] == "PT-WH-STORE"
    assert detail["partition"]["scope"] == {"city": "武汉"}
    assert detail["partition"]["status"] == PARTITION_NOT_BUILT
    # 按父级标识读出的已得事实与待回补部分
    assert detail["obtained_facts"]["parent_measures"]["revenue"] == 8000.0
    assert detail["pending_backfill"]["measures"]["revenue"] == 8000.0
    assert detail["balanced"] is True
    # 详情只带当前父级的事实行，不含相邻城市
    assert {row["city"] for row in detail["fact_rows"]} == {"武汉"}


def test_partition_scope_matches_isolates_adjacent_parent() -> None:
    partition = CubePartition(
        partition_id="P", cube_name=CUBE_NAME, level_dimension=LEVEL_DIMENSION,
        scope={"city": "武汉"},
    )
    assert partition.scope_matches({"city": "武汉"}) is True
    assert partition.scope_matches({"city": "杭州"}) is False


# ---------------------------------------------------------------------------
# 缺事实分区：构建失败 -> 重试成功，相邻父级隔离
# ---------------------------------------------------------------------------

def test_failed_build_keeps_gap_and_balance(registry: DrillGapRegistry) -> None:
    wh_id = _gap_ids(registry, "武汉")
    wh_gap = registry.get_gap(wh_id)
    result = registry.retry_partition_build(wh_id, fail=True, failure_reason="资源不足")
    assert result["ok"] is True
    partition = registry.get_partition({"city": "武汉"})
    assert partition.status == PARTITION_FAILED
    assert wh_gap.status == GAP_STATUS_FAILED
    # 失败不改变金额，待归类收入仍在且可对账
    assert wh_gap.classified_measures["revenue"] == 0.0
    assert wh_gap.pending_measures["revenue"] == 8000.0
    assert wh_gap.is_balanced()


def test_retry_success_rejects_adjacent_parent_rows(registry: DrillGapRegistry) -> None:
    wh_id = _gap_ids(registry, "武汉")
    hz_gap = registry.get_gap(_gap_ids(registry, "杭州"))
    incoming = [
        {"city": "武汉", "store": "武汉光谷店", "revenue": 5000.0, "order_count": 50.0},
        {"city": "武汉", "store": "武汉江汉店", "revenue": 3000.0, "order_count": 30.0},
        # 相邻父级（杭州）的分区数据不得带入武汉门店层
        {"city": "杭州", "store": "杭州越界店", "revenue": 9999.0, "order_count": 99.0},
    ]
    registry.retry_partition_build(wh_id, fail=True)
    result = registry.retry_partition_build(wh_id, fail=False, incoming_rows=incoming)
    assert result["ok"] is True

    wh_gap = registry.get_gap(wh_id)
    partition = registry.get_partition({"city": "武汉"})
    history = wh_gap.history[-1]
    assert history["accepted_rows"] == 2
    assert history["rejected_adjacent_rows"] == 1
    assert partition.status == PARTITION_ONLINE
    # 武汉分区只核算本父级 8000，杭州 9999 被拒收
    assert partition.classified_measures["revenue"] == 8000.0
    assert wh_gap.classified_measures["revenue"] == 8000.0
    assert wh_gap.pending_measures["revenue"] == 0.0
    assert wh_gap.status == GAP_STATUS_CLASSIFIED
    assert wh_gap.is_balanced()
    # 杭州缺层金额不受影响
    assert hz_gap.classified_measures["revenue"] == 7000.0
    assert hz_gap.pending_measures["revenue"] == 3000.0


def test_retry_wrong_reason_type_rejected(registry: DrillGapRegistry) -> None:
    hz_id = _gap_ids(registry, "杭州")
    result = registry.retry_partition_build(hz_id)
    assert result["ok"] is False
    assert "缺事实分区" in result["message"]


# ---------------------------------------------------------------------------
# 缺维属性：补齐门店维值，待归类收入归位
# ---------------------------------------------------------------------------

def test_attribute_backfill_classifies_pending_revenue(registry: DrillGapRegistry) -> None:
    hz_id = _gap_ids(registry, "杭州")
    hz_gap = registry.get_gap(hz_id)
    result = registry.backfill_attribute(hz_id)
    assert result["ok"] is True
    assert hz_gap.status == GAP_STATUS_CLASSIFIED
    assert hz_gap.classified_measures == {"revenue": 10000.0, "order_count": 100.0}
    assert hz_gap.pending_measures == {"revenue": 0.0, "order_count": 0.0}
    assert hz_gap.is_balanced()
    # 事实行门店维值被实际补齐，可进入门店层
    assert all(str(row.get("store") or "").strip() for row in registry.fact_rows_for("杭州"))


def test_backfill_wrong_reason_type_rejected(registry: DrillGapRegistry) -> None:
    wh_id = _gap_ids(registry, "武汉")
    result = registry.backfill_attribute(wh_id)
    assert result["ok"] is False
    assert "缺维属性" in result["message"]


# ---------------------------------------------------------------------------
# 全量对账与纯函数行为
# ---------------------------------------------------------------------------

def test_reconciliation_report_all_balanced(registry: DrillGapRegistry) -> None:
    registry.backfill_attribute(_gap_ids(registry, "杭州"))
    registry.retry_partition_build(_gap_ids(registry, "武汉"))
    report = registry.reconciliation_report()
    assert report["all_balanced"] is True
    for item in report["items"]:
        for name in MEASURE_NAMES:
            assert item["reconciliation"][name] == item["parent_measures"][name]


def test_build_gap_record_unknown_parent_has_zero_parent_measures() -> None:
    gap = build_drill_gap_record(
        cube_name=CUBE_NAME,
        parent_dimension=PARENT_DIMENSION,
        parent_value="南京",
        level_dimension=LEVEL_DIMENSION,
        measure_names=list(MEASURE_NAMES),
        parent_facts=DEMO_PARENT_FACTS,
        fact_rows=DEMO_FACT_ROWS,
        gap_id="DG-X",
        partitions=[],
    )
    # 无父行事实时金额为 0 且仍满足对账恒等式
    assert gap.parent_measures == {"revenue": 0.0, "order_count": 0.0}
    assert gap.is_balanced()
    assert gap.reason_code == MISSING_FACT_PARTITION


def test_reset_restores_demo_scenario(registry: DrillGapRegistry) -> None:
    registry.backfill_attribute(_gap_ids(registry, "杭州"))
    registry.reset()
    gaps = {g["parent_value"]: g for g in registry.list_gaps()}
    assert gaps["杭州"]["status"] == "待回补"
    assert gaps["武汉"]["status"] == "待回补"
    assert gaps["武汉"]["pending_measures"]["revenue"] == 8000.0
