"""下钻路径领域服务：层级分析、度量聚合、看板联动、空层缺层登记与回补。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store
from app.analytics_engine import compute_drill_levels
from app.services.drill_gap_registry import drill_gap_registry


class DrillPathService(BaseModuleService):
    MODULE = "drill_path"
    ENTITY = "下钻配置"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["路径名称", "起始维度", "下钻层级", "各层度量", "是否启用手动", "关联看板", "路径说明"]
    REQUIRED_FIELDS = ["路径名称", "起始维度", "下钻层级"]
    STATUS_ORDER = ["草稿", "已启用", "已停用"]
    ACTION_RULES = {"启用路径": "已启用", "停用路径": "已停用"}
    NEGATIVE_ACTIONS = ["停用路径"]

    # ---- 业务方法 ----

    def execute_drill(
        self, path_id: int, data: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """执行下钻分析：逐层聚合度量。"""
        path_config = store.find(self.MODULE, path_id)
        if path_config is None:
            return {"ok": False, "message": f"下钻配置 {path_id} 不存在"}

        hierarchy_raw = str(path_config.get("下钻层级", ""))
        hierarchy = [h.strip() for h in hierarchy_raw.split(",") if h.strip()]
        measures_raw = str(path_config.get("各层度量", ""))
        measures = [m.strip() for m in measures_raw.split(",") if m.strip()]

        levels = compute_drill_levels(data, hierarchy, measures)

        return {
            "ok": True,
            "path_name": str(path_config.get("路径名称", "")),
            "start_dimension": path_config.get("起始维度"),
            "hierarchy": hierarchy,
            "measures": measures,
            "levels": levels,
            "level_count": len(levels),
            "associated_dashboard": path_config.get("关联看板"),
        }

    def get_drill_paths_for_dashboard(self, dashboard_name: str) -> list[dict[str, Any]]:
        """获取某看板关联的下钻路径（数据范围→下钻路径消费）。"""
        return [
            row for row in store.rows(self.MODULE)
            if row.get("关联看板") == dashboard_name and row.get("status") == "已启用"
        ]

    def get_enabled_paths(self) -> list[dict[str, Any]]:
        """获取所有已启用路径。"""
        return [
            row for row in store.rows(self.MODULE)
            if row.get("status") == "已启用"
        ]

    def compare_drill_results(
        self, path_ids: list[int], data: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """比较多条下钻路径的分析结果。"""
        results: list[dict[str, Any]] = []
        for pid in path_ids:
            r = self.execute_drill(pid, data)
            if r.get("ok"):
                results.append(r)
        return {
            "ok": True,
            "path_count": len(path_ids),
            "results": results,
        }

    # ---- 城市下钻空层（缺层）----

    def list_drill_gaps(
        self, reason_code: str | None = None, status: str | None = None,
    ) -> dict[str, Any]:
        """缺层清单：聚合空层对应父级事实的下钻缺层数据。"""
        return {
            "ok": True,
            "items": drill_gap_registry.list_gaps(reason_code=reason_code, status=status),
            "total": len(drill_gap_registry.list_gaps(reason_code=reason_code, status=status)),
            "explanations": drill_gap_registry.empty_layer_explanations(),
        }

    def get_drill_gap_detail(self, gap_id: str) -> dict[str, Any]:
        """下钻详情页：缺层数据关联立方分区，读出已得事实与仍需回补部分。"""
        return drill_gap_registry.gap_detail(gap_id)

    def backfill_gap_attribute(self, gap_id: str) -> dict[str, Any]:
        """缺维属性回补：补齐门店维值，待归类收入归位。"""
        return drill_gap_registry.backfill_attribute(gap_id)

    def retry_gap_partition(
        self, gap_id: str, *, fail: bool = False, failure_reason: str = "",
        incoming_rows: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """缺事实分区重试：立方构建失败/成功，相邻父级分区不得带入。"""
        return drill_gap_registry.retry_partition_build(
            gap_id, fail=fail, failure_reason=failure_reason,
            incoming_rows=incoming_rows,
        )

    def gap_reconciliation(self) -> dict[str, Any]:
        """已算金额与待归类收入对账。"""
        return drill_gap_registry.reconciliation_report()

    def list_cube_partitions(self) -> dict[str, Any]:
        """立方分区视图：按父级标识查看门店层分区状态。"""
        return {"ok": True, "items": drill_gap_registry.list_partitions()}

    def reset_drill_gaps(self) -> dict[str, Any]:
        """把演示缺层场景恢复到初始状态（测试/演示用）。"""
        drill_gap_registry.reset()
        return {"ok": True, "message": "下钻缺层演示数据已重置"}

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["artifact"] = "下钻路径数据集"
        base["enabled_paths"] = sum(1 for row in store.rows(self.MODULE) if row.get("status") == "已启用")
        base["drill_gap_count"] = len(drill_gap_registry.list_gaps())
        base["drill_gap_pending"] = len(drill_gap_registry.gaps_by_status("待回补"))
        return base