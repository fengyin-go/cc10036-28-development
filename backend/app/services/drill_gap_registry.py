"""下钻空层（缺层）运行时登记处。

承载城市下钻到门店层时出现的两类空层：
- 杭州：订单事实已收到，但部分事实行门店维值为空（缺维属性）；
- 武汉：门店层立方分区尚未构建（缺事实分区），构建失败后可重试。

登记处在内存中维护缺层记录、立方分区和原始事实行；所有分区读取与重试都
按父级标识（城市）严格隔离，避免相邻父级分区串入当前门店层。
"""
from __future__ import annotations

from typing import Any

from app.analytics_engine import (
    CubePartition,
    DrillGapRecord,
    GAP_STATUS_CLASSIFIED,
    GAP_STATUS_FAILED,
    GAP_STATUS_PENDING,
    MISSING_DIMENSION_ATTR,
    MISSING_FACT_PARTITION,
    PARTITION_FAILED,
    PARTITION_NOT_BUILT,
    PARTITION_ONLINE,
    backfill_dimension_gap,
    build_drill_gap_record,
    drill_gap_detail,
    find_partition,
    retry_fact_partition,
)

CUBE_NAME = "交易分析立方"
PARENT_DIMENSION = "city"
LEVEL_DIMENSION = "store"
MEASURE_NAMES = ["revenue", "order_count"]

# 演示用父级事实：父行已经核算的收入，空层登记后必须原样保留。
DEMO_PARENT_FACTS: list[dict[str, Any]] = [
    {"city": "杭州", "revenue": 10000.0, "order_count": 100.0},
    {"city": "武汉", "revenue": 8000.0, "order_count": 80.0},
]

# 演示用明细事实行。
# 杭州：门店维值存在空值（缺维属性），订单已收到但无法归位到门店层。
# 武汉：所有行都有门店维值，但对应门店层立方分区尚未构建（缺事实分区）。
DEMO_FACT_ROWS: list[dict[str, Any]] = [
    # 杭州：合计 10000 / 100，其中 3000 / 30 缺门店维值
    {"city": "杭州", "store": "杭州湖滨店", "revenue": 4000.0, "order_count": 40.0},
    {"city": "杭州", "store": "杭州西湖店", "revenue": 3000.0, "order_count": 30.0},
    {"city": "杭州", "store": "", "revenue": 1800.0, "order_count": 18.0},
    {"city": "杭州", "store": None, "revenue": 1200.0, "order_count": 12.0},
    # 武汉：合计 8000 / 80，维值齐全，缺的是立方分区
    {"city": "武汉", "store": "武汉光谷店", "revenue": 5000.0, "order_count": 50.0},
    {"city": "武汉", "store": "武汉江汉店", "revenue": 3000.0, "order_count": 30.0},
]


class DrillGapRegistry:
    """下钻缺层数据、立方分区与事实行的内存登记处。"""

    def __init__(self) -> None:
        self._gaps: dict[str, DrillGapRecord] = {}
        self._partitions: list[CubePartition] = []
        self._fact_rows: list[dict[str, Any]] = []
        self._parent_facts: list[dict[str, Any]] = []
        self._seq = 0
        self.seed_demo()

    # ---- 初始化 ----

    def seed_demo(self) -> None:
        """灌入两座城市、两种空层原因的演示场景。"""
        self._gaps.clear()
        self._partitions.clear()
        self._fact_rows = [dict(row) for row in DEMO_FACT_ROWS]
        self._parent_facts = [dict(row) for row in DEMO_PARENT_FACTS]
        self._seq = 0

        self._partitions.append(CubePartition(
            partition_id="PT-HZ-STORE",
            cube_name=CUBE_NAME,
            level_dimension=LEVEL_DIMENSION,
            scope={"city": "杭州"},
            status=PARTITION_ONLINE,
            classified_measures={"revenue": 7000.0, "order_count": 70.0},
        ))
        self._partitions.append(CubePartition(
            partition_id="PT-WH-STORE",
            cube_name=CUBE_NAME,
            level_dimension=LEVEL_DIMENSION,
            scope={"city": "武汉"},
            status=PARTITION_NOT_BUILT,
            classified_measures={},
        ))

        self.register_gap("杭州", MISSING_DIMENSION_ATTR)
        self.register_gap("武汉", MISSING_FACT_PARTITION)

    def reset(self) -> None:
        self.seed_demo()

    # ---- 登记与查询 ----

    def _next_gap_id(self) -> str:
        self._seq += 1
        return f"DG-{self._seq:04d}"

    def register_gap(self, parent_value: str, reason_code: str) -> DrillGapRecord:
        """对指定父级发起下钻并登记缺层数据，保留父行收入与空层原因。"""
        gap_id = self._next_gap_id()
        gap = build_drill_gap_record(
            cube_name=CUBE_NAME,
            parent_dimension=PARENT_DIMENSION,
            parent_value=parent_value,
            level_dimension=LEVEL_DIMENSION,
            measure_names=list(MEASURE_NAMES),
            parent_facts=self._parent_facts,
            fact_rows=self._fact_rows,
            gap_id=gap_id,
            partitions=self._partitions,
        )
        # 允许调用方显式指定空层原因（演示场景按城市固定原因）。
        if reason_code != gap.reason_code:
            from app.analytics_engine import GAP_REASON_LABELS
            gap.reason_code = reason_code
            gap.reason_label = GAP_REASON_LABELS[reason_code]
            gap.history.append({"event": "校准空层原因", "reason_code": reason_code})
        self._gaps[gap_id] = gap
        return gap

    def list_gaps(
        self, reason_code: str | None = None, status: str | None = None,
    ) -> list[dict[str, Any]]:
        records = list(self._gaps.values())
        if reason_code:
            records = [g for g in records if g.reason_code == reason_code]
        if status:
            records = [g for g in records if g.status == status]
        return [g.to_dict() for g in records]

    def get_gap(self, gap_id: str) -> DrillGapRecord | None:
        return self._gaps.get(gap_id)

    def get_partition(self, parent_key: dict[str, Any]) -> CubePartition | None:
        return find_partition(self._partitions, parent_key, LEVEL_DIMENSION)

    def list_partitions(self) -> list[dict[str, Any]]:
        return [p.to_dict() for p in self._partitions]

    def fact_rows_for(self, parent_value: str) -> list[dict[str, Any]]:
        """读取某父级事实行副本，供前端/测试核对明细。"""
        return [
            dict(row) for row in self._fact_rows
            if str(row.get(PARENT_DIMENSION, "")) == str(parent_value)
        ]

    # ---- 下钻详情页 ----

    def gap_detail(self, gap_id: str) -> dict[str, Any]:
        gap = self.get_gap(gap_id)
        if gap is None:
            return {"ok": False, "message": f"缺层记录 {gap_id} 不存在"}
        detail = drill_gap_detail(gap, self._partitions)
        detail["ok"] = True
        detail["fact_rows"] = self.fact_rows_for(gap.parent_value)
        return detail

    # ---- 回补动作 ----

    def backfill_attribute(self, gap_id: str, store_value_field: str = "store") -> dict[str, Any]:
        """缺维属性：补齐门店维值，待归类收入归位到门店层。"""
        gap = self.get_gap(gap_id)
        if gap is None:
            return {"ok": False, "message": f"缺层记录 {gap_id} 不存在"}
        if gap.reason_code != MISSING_DIMENSION_ATTR:
            return {"ok": False, "message": "该缺层不是缺维属性，不能做维值回补"}
        backfill_dimension_gap(gap, self._fact_rows, store_value_field=store_value_field)
        return {"ok": True, "detail": self.gap_detail(gap_id)}

    def retry_partition_build(
        self, gap_id: str, *, fail: bool = False, failure_reason: str = "",
        incoming_rows: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """缺事实分区：重试立方分区构建，严格隔离相邻父级数据。

        incoming_rows 缺省使用登记处内的事实行；测试/外部批次可显式传入，
        混入的相邻父级行会在分区入口按父级标识被拒绝并计入拒收数。
        """
        gap = self.get_gap(gap_id)
        if gap is None:
            return {"ok": False, "message": f"缺层记录 {gap_id} 不存在"}
        if gap.reason_code != MISSING_FACT_PARTITION:
            return {"ok": False, "message": "该缺层不是缺事实分区，不能做分区构建"}
        try:
            retry_fact_partition(
                gap, self._partitions,
                incoming_rows if incoming_rows is not None else self._fact_rows,
                fail=fail, failure_reason=failure_reason,
            )
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "detail": self.gap_detail(gap_id)}

    # ---- 对账 ----

    def reconciliation_report(self) -> dict[str, Any]:
        """全量对账：每个缺层父行已算金额 = 已归类 + 待归类。"""
        items: list[dict[str, Any]] = []
        all_balanced = True
        for gap in self._gaps.values():
            balanced = gap.is_balanced()
            all_balanced = all_balanced and balanced
            items.append({
                "gap_id": gap.gap_id,
                "parent_value": gap.parent_value,
                "reason_code": gap.reason_code,
                "status": gap.status,
                "parent_measures": dict(gap.parent_measures),
                "classified_measures": dict(gap.classified_measures),
                "pending_measures": dict(gap.pending_measures),
                "reconciliation": gap.reconciliation(),
                "balanced": balanced,
            })
        return {"ok": True, "all_balanced": all_balanced, "items": items}

    def empty_layer_explanations(self) -> list[dict[str, Any]]:
        """各类空层解释清单：原因码、中文说明、涉及父级与当前状态。"""
        from app.analytics_engine import GAP_REASON_LABELS
        return [
            {
                "reason_code": code,
                "reason_label": GAP_REASON_LABELS[code],
                "affected_parents": [
                    g.parent_value for g in self._gaps.values() if g.reason_code == code
                ],
                "gap_count": sum(
                    1 for g in self._gaps.values() if g.reason_code == code
                ),
                "pending_count": sum(
                    1 for g in self._gaps.values()
                    if g.reason_code == code and g.status != GAP_STATUS_CLASSIFIED
                ),
            }
            for code in (MISSING_DIMENSION_ATTR, MISSING_FACT_PARTITION)
        ]

    # 便于测试按状态过滤
    def gaps_by_status(self, status: str) -> list[DrillGapRecord]:
        return [g for g in self._gaps.values() if g.status == status]


# 单例登记处，服务与路由共用同一份内存状态。
drill_gap_registry = DrillGapRegistry()
