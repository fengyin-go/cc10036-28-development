"""多维分析查询引擎：提供跨模块共享的聚合、归因、保留、检测和血缘计算核心。

所有计算方法均为纯函数，不依赖 Store，方便按业务模块组合调用。
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable

# ---------------------------------------------------------------------------
# 通用聚合函数
# ---------------------------------------------------------------------------

AGGREGATORS: dict[str, Callable[[list[float | int]], float]] = {
    "SUM": lambda values: sum(values),
    "AVG": lambda values: sum(values) / len(values) if values else 0.0,
    "COUNT": lambda values: float(len(values)),
    "MAX": lambda values: float(max(values)) if values else 0.0,
    "MIN": lambda values: float(min(values)) if values else 0.0,
    "DISTINCT_COUNT": lambda values: float(len(set(values))),
    "WEIGHTED_AVG": lambda values, weights=None: _weighted_avg(values, weights or []),
}


def _weighted_avg(values: list[float | int], weights: list[float | int]) -> float:
    if not values or not weights or len(values) != len(weights):
        return 0.0
    total_weight = sum(weights)
    if total_weight == 0:
        return 0.0
    return sum(v * w for v, w in zip(values, weights)) / total_weight


def compute_metric(values: list[float | int], aggregator: str) -> float:
    """按聚合函数计算指标值，口径统一入口。"""
    func = AGGREGATORS.get(aggregator)
    if func is None:
        raise ValueError(f"不支持的聚合函数：{aggregator}")
    return func(values)


# ---------------------------------------------------------------------------
# 多维立方预计算
# ---------------------------------------------------------------------------

@dataclass
class DimensionCombo:
    """维度组合记录。"""
    dimensions: list[str]
    measure_values: dict[str, float]
    row_count: int = 0


def build_cube_cell(
    raw_data: list[dict[str, Any]],
    dimensions: list[str],
    measures: list[dict[str, str]],  # [{"name": "revenue", "aggregator": "SUM"}, ...]
) -> list[DimensionCombo]:
    """从原始数据构建多维立方单元格，按维度组合分组聚合度量。"""
    group_map: dict[tuple[str, ...], dict[str, list[float | int]]] = {}
    for row in raw_data:
        dim_key = tuple(str(row.get(d, "")) for d in dimensions)
        if dim_key not in group_map:
            group_map[dim_key] = {m["name"]: [] for m in measures}
        for m in measures:
            val = row.get(m["name"])
            if val is not None:
                try:
                    group_map[dim_key][m["name"]].append(float(val))
                except (ValueError, TypeError):
                    pass

    results: list[DimensionCombo] = []
    for dim_key, measure_data in group_map.items():
        measure_values: dict[str, float] = {}
        total_rows = max(len(v) for v in measure_data.values()) if measure_data else 0
        for m in measures:
            values = measure_data.get(m["name"], [])
            measure_values[m["name"]] = compute_metric(values, m["aggregator"])
        results.append(DimensionCombo(
            dimensions=list(dim_key),
            measure_values=measure_values,
            row_count=total_rows,
        ))
    return results


def generate_dimension_combinations(dimensions: list[str]) -> list[list[str]]:
    """生成所有维度组合（用于预计算Cube），含全量组合。"""
    from itertools import combinations
    combos: list[list[str]] = []
    for r in range(1, len(dimensions) + 1):
        for combo in combinations(dimensions, r):
            combos.append(list(combo))
    return combos


# ---------------------------------------------------------------------------
# 分群条件解析
# ---------------------------------------------------------------------------

@dataclass
class FilterCondition:
    field: str
    operator: str  # eq, ne, gt, lt, gte, lte, in, not_in, contains, starts_with
    value: Any


def parse_segment_conditions(condition_expr: str) -> list[FilterCondition]:
    """解析分群筛选条件表达式。格式：field:op:value;field:op:value"""
    conditions: list[FilterCondition] = []
    if not condition_expr:
        return conditions
    for part in condition_expr.split(";"):
        part = part.strip()
        if not part:
            continue
        components = part.split(":", 2)
        if len(components) >= 3:
            conditions.append(FilterCondition(
                field=components[0].strip(),
                operator=components[1].strip(),
                value=components[2].strip(),
            ))
    return conditions


def apply_filter(row: dict[str, Any], condition: FilterCondition) -> bool:
    """对单行数据应用筛选条件。"""
    cell_val = row.get(condition.field)
    op = condition.operator
    val = condition.value

    try:
        if op == "eq":
            return str(cell_val) == str(val)
        elif op == "ne":
            return str(cell_val) != str(val)
        elif op in ("gt", "gte", "lt", "lte"):
            cell_num = float(cell_val) if cell_val is not None else 0
            val_num = float(val)
            if op == "gt":
                return cell_num > val_num
            elif op == "gte":
                return cell_num >= val_num
            elif op == "lt":
                return cell_num < val_num
            elif op == "lte":
                return cell_num <= val_num
        elif op == "in":
            allowed = set(v.strip() for v in str(val).split(","))
            return str(cell_val) in allowed
        elif op == "not_in":
            denied = set(v.strip() for v in str(val).split(","))
            return str(cell_val) not in denied
        elif op == "contains":
            return str(val).casefold() in str(cell_val).casefold()
        elif op == "starts_with":
            return str(cell_val or "").startswith(str(val))
        elif op == "between":
            bounds = str(val).split(",")
            if len(bounds) == 2:
                cell_num = float(cell_val) if cell_val is not None else 0
                return float(bounds[0]) <= cell_num <= float(bounds[1])
        return False
    except (ValueError, TypeError):
        return False


def segment_population(
    population: list[dict[str, Any]],
    include_conditions: list[FilterCondition],
    exclude_conditions: list[FilterCondition],
    intersect_strategy: str = "all",  # all / any
) -> tuple[list[dict[str, Any]], int]:
    """对人群执行分群圈选，返回命中的行和命中数。"""
    results: list[dict[str, Any]] = []
    for row in population:
        if include_conditions:
            if intersect_strategy == "all":
                if not all(apply_filter(row, c) for c in include_conditions):
                    continue
            else:
                if not any(apply_filter(row, c) for c in include_conditions):
                    continue
        if exclude_conditions and any(apply_filter(row, c) for c in exclude_conditions):
            continue
        results.append(row)
    return results, len(results)


# ---------------------------------------------------------------------------
# 同期群保留率计算
# ---------------------------------------------------------------------------

@dataclass
class CohortMatrix:
    """同期群矩阵：行=同期群（按初始事件时间划分），列=周期偏移。"""
    cohort_ids: list[str]
    period_labels: list[str]
    retention_matrix: list[list[float]]  # [cohort_index][period_index] = 保留率


def compute_cohort_retention(
    events: list[dict[str, Any]],
    init_event_name: str,
    return_event_name: str,
    period_days: int = 7,
    max_periods: int = 8,
) -> CohortMatrix:
    """计算同期群保留率矩阵。

    events: 每行 {user_id, event_name, event_time(epoch_seconds)}
    返回保留率矩阵（百分比 0-100）。
    """
    # 按user分组事件
    user_events: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for e in events:
        uid = str(e.get("user_id", ""))
        ename = str(e.get("event_name", ""))
        etime = float(e.get("event_time", 0))
        if uid:
            user_events[uid].append((ename, etime))

    # 为每个用户找到首次初始事件时间和回访事件时间
    user_cohorts: dict[str, float] = {}  # user -> first init time
    user_return_times: dict[str, list[float]] = defaultdict(list)
    for uid, evts in user_events.items():
        first_init: float | None = None
        returns: list[float] = []
        for ename, etime in sorted(evts, key=lambda x: x[1]):
            if first_init is None and ename == init_event_name:
                first_init = etime
            elif first_init is not None and ename == return_event_name and etime > first_init:
                returns.append(etime)
        if first_init is not None:
            user_cohorts[uid] = first_init
            user_return_times[uid] = returns

    # 按周分组同期群
    period_seconds = period_days * 86400
    cohort_buckets: dict[int, set[str]] = defaultdict(set)  # week_number -> set of user_ids
    for uid, init_time in user_cohorts.items():
        week = int(init_time // period_seconds)
        cohort_buckets[week].add(uid)

    sorted_weeks = sorted(cohort_buckets)
    cohort_ids = [_week_label(w, period_days) for w in sorted_weeks]
    matrix: list[list[float]] = []

    for wi, week in enumerate(sorted_weeks):
        users_in_cohort = cohort_buckets[week]
        total = len(users_in_cohort)
        if total == 0:
            matrix.append([0.0] * max_periods)
            continue
        cohort_start = week * period_seconds
        period_rates: list[float] = []
        for p in range(max_periods):
            period_end = cohort_start + (p + 1) * period_seconds
            retained = 0
            for uid in users_in_cohort:
                return_times = user_return_times.get(uid, [])
                if any(cohort_start < rt <= period_end for rt in return_times):
                    retained += 1
            rate = round(retained / total * 100, 2)
            period_rates.append(rate)
        matrix.append(period_rates)

    period_labels = [f"周{i + 1}" for i in range(max_periods)]
    return CohortMatrix(
        cohort_ids=cohort_ids,
        period_labels=period_labels,
        retention_matrix=matrix,
    )


def _week_label(epoch_seconds: float, period_days: int) -> str:
    import datetime
    dt = datetime.datetime.fromtimestamp(epoch_seconds, tz=datetime.timezone.utc)
    return dt.strftime("W%W-%Y")


# ---------------------------------------------------------------------------
# 归因权重分配
# ---------------------------------------------------------------------------

@dataclass
class AttributionResult:
    """归因结果：每个触点的权重分配。"""
    touchpoints: list[str]
    weights: list[float]
    model: str  # first_touch / last_touch / linear / time_decay / position_based


def compute_attribution(
    touchpoints: list[dict[str, Any]],
    model: str = "linear",
    decay_half_life_days: float = 7.0,
) -> AttributionResult:
    """计算归因权重分配。

    touchpoints: [{"name": str, "time": epoch_seconds}, ...]
    模型支持：first_touch, last_touch, linear, time_decay, position_based
    """
    if not touchpoints:
        return AttributionResult(touchpoints=[], weights=[], model=model)

    tp_names = [tp["name"] for tp in touchpoints]
    tp_times = [float(tp.get("time", 0)) for tp in touchpoints]
    n = len(touchpoints)

    if model == "first_touch":
        weights = [1.0 if i == 0 else 0.0 for i in range(n)]
    elif model == "last_touch":
        weights = [1.0 if i == n - 1 else 0.0 for i in range(n)]
    elif model == "time_decay":
        max_time = max(tp_times) if tp_times else 0
        half_life = decay_half_life_days * 86400
        raw: list[float] = []
        for t in tp_times:
            delta = max_time - t
            raw.append(2.0 ** (-delta / half_life) if half_life > 0 else 1.0)
        raw_sum = sum(raw)
        weights = [r / raw_sum if raw_sum > 0 else 1.0 / n for r in raw]
    elif model == "position_based":
        if n == 1:
            weights = [1.0]
        else:
            first_weight = 0.4
            last_weight = 0.4
            middle_share = 0.2
            middle_count = n - 2
            weights = []
            for i in range(n):
                if i == 0:
                    weights.append(first_weight)
                elif i == n - 1:
                    weights.append(last_weight)
                else:
                    weights.append(middle_share / middle_count if middle_count > 0 else 0)
    else:  # linear
        weights = [1.0 / n] * n

    # 归一化
    total = sum(weights)
    if total > 0:
        weights = [w / total for w in weights]

    return AttributionResult(touchpoints=tp_names, weights=weights, model=model)


# ---------------------------------------------------------------------------
# 血缘图结构计算
# ---------------------------------------------------------------------------

@dataclass
class LineageNode:
    node_id: str
    node_name: str
    node_type: str  # table / metric / column / report
    upstream_ids: list[str]
    downstream_ids: list[str]


@dataclass
class LineageGraph:
    nodes: dict[str, LineageNode] = field(default_factory=dict)

    def add_node(self, node: LineageNode) -> None:
        self.nodes[node.node_id] = node

    def upstream_chain(self, node_id: str, max_depth: int = 10) -> list[list[str]]:
        """获取上游链路（从当前节点到根源的所有路径）。"""
        if node_id not in self.nodes:
            return []
        chains: list[list[str]] = []
        stack: list[tuple[str, list[str]]] = [(node_id, [node_id])]
        while stack:
            current, path = stack.pop()
            if len(path) > max_depth:
                chains.append(list(path))
                continue
            node = self.nodes.get(current)
            if node and node.upstream_ids:
                upstream_found = False
                for up_id in node.upstream_ids:
                    if up_id not in path:
                        stack.append((up_id, path + [up_id]))
                        upstream_found = True
                if not upstream_found:
                    chains.append(list(path))
            else:
                chains.append(list(path))
        return chains

    def downstream_chain(self, node_id: str, max_depth: int = 10) -> list[list[str]]:
        """获取下游影响链。"""
        if node_id not in self.nodes:
            return []
        chains: list[list[str]] = []
        stack: list[tuple[str, list[str]]] = [(node_id, [node_id])]
        while stack:
            current, path = stack.pop()
            if len(path) > max_depth:
                chains.append(list(path))
                continue
            node = self.nodes.get(current)
            if node and node.downstream_ids:
                ds_found = False
                for ds_id in node.downstream_ids:
                    if ds_id not in path:
                        stack.append((ds_id, path + [ds_id]))
                        ds_found = True
                if not ds_found:
                    chains.append(list(path))
            else:
                chains.append(list(path))
        return chains

    def impact_analysis(self, node_id: str) -> list[str]:
        """计算某个节点变更影响的所有下游节点。"""
        visited: set[str] = set()
        queue: deque[str] = deque([node_id])
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            node = self.nodes.get(current)
            if node:
                for ds in node.downstream_ids:
                    if ds not in visited:
                        queue.append(ds)
        visited.discard(node_id)
        return sorted(visited)

    def to_adjacency(self) -> dict[str, Any]:
        """导出邻接表格式。"""
        adj: dict[str, list[str]] = {}
        for nid, node in self.nodes.items():
            adj[nid] = list(node.upstream_ids)
        return {"nodes": list(self.nodes), "adjacency": adj}


# ---------------------------------------------------------------------------
# 查询缓存生命周期
# ---------------------------------------------------------------------------

@dataclass
class CacheEntry:
    key: str
    result: Any
    row_count: int
    byte_size: int
    created_at: float
    ttl_seconds: int
    hit_count: int = 0
    last_hit_at: float | None = None


class CacheManager:
    """按TTL和LRU管理查询结果缓存生命周期。"""

    def __init__(self, max_entries: int = 500, max_memory_bytes: int = 200 * 1024 * 1024):
        self._entries: dict[str, CacheEntry] = {}
        self._access_order: list[str] = []  # LRU: 最近访问在末尾
        self.max_entries = max_entries
        self.max_memory = max_memory_bytes

    def compute_key(self, query_desc: str, params: dict[str, Any]) -> str:
        raw = json.dumps({"query": query_desc, "params": params}, sort_keys=True)
        return hashlib.md5(raw.encode()).hexdigest()[:16]

    def get(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if time.time() - entry.created_at > entry.ttl_seconds:
            self._evict(key)
            return None
        entry.hit_count += 1
        entry.last_hit_at = time.time()
        self._touch(key)
        return entry.result

    def put(
        self, key: str, result: Any, row_count: int = 0,
        ttl_seconds: int = 300, byte_size: int | None = None,
    ) -> None:
        if byte_size is None:
            byte_size = len(json.dumps(result, default=str).encode()) if result is not None else 0
        entry = CacheEntry(
            key=key, result=result, row_count=row_count,
            byte_size=byte_size, created_at=time.time(), ttl_seconds=ttl_seconds,
        )
        self._entries[key] = entry
        self._touch(key)
        self._enforce_limits()

    def invalidate(self, key: str) -> bool:
        return self._evict(key)

    def stats(self) -> dict[str, Any]:
        total_hits = sum(e.hit_count for e in self._entries.values())
        active = sum(1 for e in self._entries.values()
                     if time.time() - e.created_at <= e.ttl_seconds)
        total_mem = sum(e.byte_size for e in self._entries.values())
        return {
            "total": len(self._entries),
            "active": active,
            "expired": len(self._entries) - active,
            "total_hits": total_hits,
            "memory_bytes": total_mem,
        }

    def _touch(self, key: str) -> None:
        if key in self._access_order:
            self._access_order.remove(key)
        self._access_order.append(key)

    def _evict(self, key: str) -> bool:
        if key in self._entries:
            del self._entries[key]
        if key in self._access_order:
            self._access_order.remove(key)
        return True

    def _enforce_limits(self) -> None:
        while len(self._entries) > self.max_entries and self._access_order:
            lru_key = self._access_order[0]
            self._evict(lru_key)
        total_mem = sum(e.byte_size for e in self._entries.values())
        while total_mem > self.max_memory and self._access_order:
            lru_key = self._access_order[0]
            self._evict(lru_key)
            total_mem = sum(e.byte_size for e in self._entries.values())


# ---------------------------------------------------------------------------
# 异常检测
# ---------------------------------------------------------------------------

def detect_anomalies_zscore(
    values: list[float], threshold: float = 3.0
) -> list[dict[str, Any]]:
    """Z-Score 异常检测。"""
    n = len(values)
    if n < 3:
        return []
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    std = math.sqrt(variance) if variance > 0 else 1e-10
    anomalies: list[dict[str, Any]] = []
    for i, v in enumerate(values):
        z = abs(v - mean) / std
        if z > threshold:
            anomalies.append({"index": i, "value": v, "z_score": round(z, 3), "method": "zscore"})
    return anomalies


def detect_anomalies_iqr(values: list[float], multiplier: float = 1.5) -> list[dict[str, Any]]:
    """IQR 异常检测。"""
    n = len(values)
    if n < 4:
        return []
    sorted_vals = sorted(values)
    q1_idx = n // 4
    q3_idx = (3 * n) // 4
    q1 = sorted_vals[q1_idx]
    q3 = sorted_vals[q3_idx]
    iqr = q3 - q1
    if iqr == 0:
        return []
    lower = q1 - multiplier * iqr
    upper = q3 + multiplier * iqr
    anomalies: list[dict[str, Any]] = []
    for i, v in enumerate(values):
        if v < lower or v > upper:
            anomalies.append({"index": i, "value": v, "lower": lower, "upper": upper, "method": "iqr"})
    return anomalies


def detect_anomalies_moving_avg(
    values: list[float], window: int = 7, deviation_ratio: float = 0.3,
) -> list[dict[str, Any]]:
    """基于移动平均偏差的异常检测。"""
    n = len(values)
    if n < window:
        return []
    anomalies: list[dict[str, Any]] = []
    for i in range(window, n):
        window_vals = values[i - window:i]
        avg = sum(window_vals) / window
        if avg == 0:
            continue
        deviation = abs(values[i] - avg) / abs(avg)
        if deviation > deviation_ratio:
            anomalies.append({
                "index": i, "value": values[i], "moving_avg": round(avg, 3),
                "deviation_pct": round(deviation * 100, 2), "method": "moving_avg",
            })
    return anomalies


# ---------------------------------------------------------------------------
# A/B 实验统计
# ---------------------------------------------------------------------------

def compute_ab_sample_size(
    baseline_rate: float, min_detectable_effect: float,
    alpha: float = 0.05, power: float = 0.8,
) -> int:
    """计算A/B实验最小样本量。"""
    z_alpha = 1.96  # 双尾 alpha=0.05
    z_beta = 0.84   # power=0.8
    p = baseline_rate
    delta = min_detectable_effect
    n = (2 * (z_alpha + z_beta) ** 2 * p * (1 - p)) / (delta ** 2)
    return max(1, math.ceil(n))


def compute_confidence_interval(
    values_a: list[float], values_b: list[float], confidence: float = 0.95,
) -> dict[str, Any]:
    """计算两组数据的置信区间和均值差异。"""
    na, nb = len(values_a), len(values_b)
    if na < 2 or nb < 2:
        return {"error": "样本量不足"}

    mean_a = sum(values_a) / na
    mean_b = sum(values_b) / nb
    diff = mean_b - mean_a

    var_a = sum((v - mean_a) ** 2 for v in values_a) / (na - 1)
    var_b = sum((v - mean_b) ** 2 for v in values_b) / (nb - 1)

    se = math.sqrt(var_a / na + var_b / nb)
    if se == 0:
        return {"mean_a": mean_a, "mean_b": mean_b, "diff": diff, "significant": False, "reason": "零方差"}

    z_value = 1.96 if confidence == 0.95 else 2.576  # 简化

    ci_lower = diff - z_value * se
    ci_upper = diff + z_value * se
    significant = (ci_lower > 0 or ci_upper < 0)

    return {
        "mean_a": round(mean_a, 4),
        "mean_b": round(mean_b, 4),
        "diff": round(diff, 4),
        "diff_pct": round(diff / (abs(mean_a) if mean_a != 0 else 1) * 100, 2),
        "ci_lower": round(ci_lower, 4),
        "ci_upper": round(ci_upper, 4),
        "significant": significant,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# 下钻路径计算
# ---------------------------------------------------------------------------

def compute_drill_levels(
    data: list[dict[str, Any]],
    drill_hierarchy: list[str],  # e.g. ["region", "city", "store"]
    measures: list[str],
    aggregator: str = "SUM",
) -> list[dict[str, Any]]:
    """按层级分组计算各层度量值。"""
    results: list[dict[str, Any]] = []
    for level_idx, dim in enumerate(drill_hierarchy):
        group_map: dict[str, dict[str, list[float]]] = {}
        for row in data:
            key = str(row.get(dim, ""))
            if key not in group_map:
                group_map[key] = {m: [] for m in measures}
            for m in measures:
                try:
                    group_map[key][m].append(float(row.get(m, 0)))
                except (ValueError, TypeError):
                    pass
        level_data: dict[str, Any] = {
            "level": level_idx,
            "dimension": dim,
            "groups": [],
        }
        for key, mdata in group_map.items():
            entry = {"key": key}
            for m in measures:
                values = mdata.get(m, [])
                entry[m] = round(compute_metric(values, aggregator), 2)
            level_data["groups"].append(entry)
        results.append(level_data)
    return results


# ---------------------------------------------------------------------------
# 下钻空层（缺层）解释与立方分区回补
#
# 城市向门店层下钻时可能出现两种“空层”：
# 1. MISSING_DIMENSION_ATTR 缺维属性：订单/事实已经到达，但事实行的门店维值
#    为空（门店维属性未回填），门店层无法归位，父行收入仍需保留，记为待归类。
# 2. MISSING_FACT_PARTITION 缺事实分区：立方在当前父级（城市）的门店层分区
#    尚未构建，已得到的父级汇总事实保留，其余部分等待分区构建后回补。
# ---------------------------------------------------------------------------

MISSING_DIMENSION_ATTR = "MISSING_DIMENSION_ATTR"
MISSING_FACT_PARTITION = "MISSING_FACT_PARTITION"

# 空层原因码 -> 中文解释（空层原因）
GAP_REASON_LABELS: dict[str, str] = {
    MISSING_DIMENSION_ATTR: "缺维属性：事实已到达但门店维值缺失，收入暂未归类",
    MISSING_FACT_PARTITION: "缺事实分区：当前父级的门店层立方分区尚未构建",
}

# 缺层记录生命周期状态
GAP_STATUS_PENDING = "待回补"
GAP_STATUS_CLASSIFIED = "已补齐"
GAP_STATUS_BUILDING = "构建中"
GAP_STATUS_FAILED = "构建失败"

# 立方分区状态
PARTITION_NOT_BUILT = "未构建"
PARTITION_BUILDING = "构建中"
PARTITION_FAILED = "构建失败"
PARTITION_ONLINE = "已上线"


@dataclass
class CubePartition:
    """立方分区：按「父级维度取值 + 下钻层维度」定位的门店层分区。

    scope 为该分区覆盖的父级标识（例如 {"city": "武汉"}），分区隔离的关键是
    读取和重试都只匹配本分区 scope，不能把相邻父级（其它城市）的分区带进来。
    """
    partition_id: str
    cube_name: str
    level_dimension: str
    scope: dict[str, Any]
    status: str = PARTITION_NOT_BUILT
    classified_measures: dict[str, float] = field(default_factory=dict)
    attempt: int = 0
    last_error: str = ""

    def scope_matches(self, parent_key: dict[str, Any]) -> bool:
        """严格按父级标识判定分区归属，杜绝相邻父级分区串入。"""
        return all(str(self.scope.get(k)) == str(v) for k, v in parent_key.items())

    def to_dict(self) -> dict[str, Any]:
        return {
            "partition_id": self.partition_id,
            "cube_name": self.cube_name,
            "level_dimension": self.level_dimension,
            "scope": dict(self.scope),
            "status": self.status,
            "classified_measures": dict(self.classified_measures),
            "attempt": self.attempt,
            "last_error": self.last_error,
        }


@dataclass
class DrillGapRecord:
    """下钻缺层记录：聚合空层对应父级事实的缺层数据。

    保留父行已经核算的收入（parent_measures / parent_revenue）与空层原因；
    classified_measures 为已归位到门店层的金额，pending_measures 为仍需回补、
    暂不能进入门店层的待归类收入。对账恒等式：
        parent_measure == classified_measure + pending_measure
    """
    gap_id: str
    cube_name: str
    parent_dimension: str
    parent_value: str
    parent_key: dict[str, Any]
    level_dimension: str
    reason_code: str
    reason_label: str
    measure_names: list[str]
    parent_measures: dict[str, float]
    classified_measures: dict[str, float]
    pending_measures: dict[str, float]
    status: str = GAP_STATUS_PENDING
    partition_id: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def parent_revenue(self) -> float:
        """父行已经核算的收入（默认取第一个度量，兼容只传金额的调用方）。"""
        if not self.measure_names:
            return 0.0
        return round(float(self.parent_measures.get(self.measure_names[0], 0.0)), 2)

    def reconciliation(self) -> dict[str, float]:
        """对账：父级金额应等于已归类金额与待归类金额之和。"""
        return {
            name: round(
                float(self.classified_measures.get(name, 0.0))
                + float(self.pending_measures.get(name, 0.0)),
                2,
            )
            for name in self.measure_names
        }

    def is_balanced(self, tolerance: float = 0.01) -> bool:
        for name in self.measure_names:
            total = float(self.classified_measures.get(name, 0.0)) + float(
                self.pending_measures.get(name, 0.0)
            )
            if abs(total - float(self.parent_measures.get(name, 0.0))) > tolerance:
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "gap_id": self.gap_id,
            "cube_name": self.cube_name,
            "parent_dimension": self.parent_dimension,
            "parent_value": self.parent_value,
            "parent_key": dict(self.parent_key),
            "level_dimension": self.level_dimension,
            "reason_code": self.reason_code,
            "reason_label": self.reason_label,
            "empty_layer_reason": self.reason_label,
            "measure_names": list(self.measure_names),
            "parent_measures": dict(self.parent_measures),
            "parent_revenue": self.parent_revenue,
            "classified_measures": dict(self.classified_measures),
            "pending_measures": dict(self.pending_measures),
            "status": self.status,
            "partition_id": self.partition_id,
            "reconciliation": self.reconciliation(),
            "balanced": self.is_balanced(),
            "history": list(self.history),
        }


def is_blank_dimension_value(row: dict[str, Any], dimension: str) -> bool:
    """判定事实行在某维度上的维值是否缺失（None / 空串 / 空白）。"""
    if dimension not in row:
        return True
    value = row.get(dimension)
    return value is None or str(value).strip() == ""


def find_parent_fact(
    parent_facts: list[dict[str, Any]],
    parent_dimension: str,
    parent_value: str,
) -> dict[str, Any] | None:
    """按父级标识从父行事实集合中读出已经核算的父级事实。"""
    for row in parent_facts:
        if str(row.get(parent_dimension, "")) == str(parent_value):
            return row
    return None


def rows_for_parent(
    fact_rows: list[dict[str, Any]],
    parent_dimension: str,
    parent_value: str,
) -> list[dict[str, Any]]:
    """只取属于当前父级的事实行；相邻父级的行在入口处即被排除。"""
    return [
        row for row in fact_rows
        if str(row.get(parent_dimension, "")) == str(parent_value)
    ]


def build_drill_gap_record(
    cube_name: str,
    parent_dimension: str,
    parent_value: str,
    level_dimension: str,
    measure_names: list[str],
    parent_facts: list[dict[str, Any]],
    fact_rows: list[dict[str, Any]],
    gap_id: str,
    partitions: list[CubePartition] | None = None,
) -> DrillGapRecord:
    """构建城市下钻空层的缺层记录。

    空层原因判定：
    - 属于当前父级的事实行里存在空门店维值 -> 缺维属性；
    - 否则若该父级的门店层立方分区未上线/未构建 -> 缺事实分区。

    父行金额来自 parent_facts（已经核算的收入，原样保留）。已归类金额：
    - 缺维属性：取门店维值非空事实行的 SUM（分区在线，明细可直接归位）；
    - 缺事实分区：以立方分区已核算度量为准，分区未构建时为 0（明细虽有门店
      维值但尚未进入门店层）。待归类金额 = 父行金额 - 已归类金额，保证可对账。
    """
    parent_key = {parent_dimension: parent_value}
    parent_fact = find_parent_fact(parent_facts, parent_dimension, parent_value) or {}
    parent_measures = {
        name: round(float(parent_fact.get(name, 0.0) or 0.0), 2)
        for name in measure_names
    }

    scoped_rows = rows_for_parent(fact_rows, parent_dimension, parent_value)
    blank_rows = [r for r in scoped_rows if is_blank_dimension_value(r, level_dimension)]
    has_missing_attr = bool(blank_rows)

    partition = next(
        (p for p in (partitions or [])
         if p.scope_matches(parent_key) and p.level_dimension == level_dimension),
        None,
    )
    partition_id = partition.partition_id if partition is not None else ""

    if has_missing_attr:
        reason_code = MISSING_DIMENSION_ATTR
    else:
        reason_code = MISSING_FACT_PARTITION

    # 已归位事实：属于本父级且门店维值非空，按度量求和。
    classified_rows = [r for r in scoped_rows if not is_blank_dimension_value(r, level_dimension)]
    row_classified: dict[str, float] = {}
    for name in measure_names:
        values = [
            float(r.get(name, 0.0) or 0.0)
            for r in classified_rows
            if r.get(name) is not None
        ]
        row_classified[name] = round(sum(values), 2)

    if reason_code == MISSING_DIMENSION_ATTR:
        # 缺维属性：门店层分区在线，已归位金额直接取非空门店维值事实行。
        classified_measures = dict(row_classified)
    else:
        # 缺事实分区：门店层分区未构建，明细行虽有门店维值也尚未进入门店层，
        # 已归位金额只能以分区已核算度量为准（未构建分区为 0）。
        classified_measures = {
            name: round(float(partition.classified_measures.get(name, 0.0)), 2)
            if partition is not None else 0.0
            for name in measure_names
        }

    pending_measures = {
        name: round(parent_measures[name] - classified_measures.get(name, 0.0), 2)
        for name in measure_names
    }

    return DrillGapRecord(
        gap_id=gap_id,
        cube_name=cube_name,
        parent_dimension=parent_dimension,
        parent_value=str(parent_value),
        parent_key=parent_key,
        level_dimension=level_dimension,
        reason_code=reason_code,
        reason_label=GAP_REASON_LABELS[reason_code],
        measure_names=list(measure_names),
        parent_measures=parent_measures,
        classified_measures=classified_measures,
        pending_measures=pending_measures,
        status=GAP_STATUS_PENDING,
        partition_id=partition_id,
        history=[{
            "event": "登记缺层",
            "reason_code": reason_code,
            "parent_revenue": parent_measures,
        }],
    )


def find_partition(
    partitions: list[CubePartition],
    parent_key: dict[str, Any],
    level_dimension: str,
) -> CubePartition | None:
    """按父级标识关联立方分区；相邻父级分区不会被读出。"""
    for partition in partitions:
        if partition.level_dimension == level_dimension and partition.scope_matches(parent_key):
            return partition
    return None


def backfill_dimension_gap(
    gap: DrillGapRecord,
    fact_rows: list[dict[str, Any]],
    store_value_field: str = "store",
) -> DrillGapRecord:
    """缺维属性回补：事实已在本地，补齐门店维值后把待归类收入归位。

    只处理属于当前父级且门店维值原本为空的行，任何相邻父级行都不参与，
    归位前后父行金额不变（classified + pending 恒等于 parent）。
    """
    if gap.reason_code != MISSING_DIMENSION_ATTR:
        raise ValueError("仅缺维属性缺层支持维值回补")

    scoped_rows = rows_for_parent(fact_rows, gap.parent_dimension, gap.parent_value)
    moved: dict[str, float] = {name: 0.0 for name in gap.measure_names}
    backfilled_rows = 0
    for row in scoped_rows:
        if not is_blank_dimension_value(row, gap.level_dimension):
            continue
        assigned = str(row.get(store_value_field) or "").strip() or f"回补门店_{gap.parent_value}"
        row[gap.level_dimension] = assigned
        backfilled_rows += 1
        for name in gap.measure_names:
            moved[name] += float(row.get(name, 0.0) or 0.0)

    for name in gap.measure_names:
        moved[name] = round(moved[name], 2)
        gap.classified_measures[name] = round(
            float(gap.classified_measures.get(name, 0.0)) + moved[name], 2
        )
        gap.pending_measures[name] = round(
            float(gap.parent_measures.get(name, 0.0)) - gap.classified_measures[name], 2
        )

    gap.status = GAP_STATUS_CLASSIFIED
    gap.history.append({
        "event": "维值回补",
        "backfilled_rows": backfilled_rows,
        "moved_measures": dict(moved),
    })
    return gap


def retry_fact_partition(
    gap: DrillGapRecord,
    partitions: list[CubePartition],
    incoming_rows: list[dict[str, Any]],
    *,
    fail: bool = False,
    failure_reason: str = "",
) -> DrillGapRecord:
    """缺事实分区的立方构建重试。

    重试严格按 gap.parent_key 过滤 incoming_rows：只有当前父级（城市）的事实
    能进入本门店层分区，相邻父级分区在过滤阶段就被挡在外面。fail=True 模拟
    构建失败：分区标记失败、缺层保留待回补，金额对账关系不变。
    """
    if gap.reason_code != MISSING_FACT_PARTITION:
        raise ValueError("仅缺事实分区缺层支持分区构建重试")

    partition = find_partition(partitions, gap.parent_key, gap.level_dimension)
    if partition is None:
        raise ValueError(
            f"父级 {gap.parent_key} 的 {gap.level_dimension} 层分区不存在"
        )

    partition.attempt += 1
    scoped_rows = [
        row for row in incoming_rows
        if all(str(row.get(k, "")) == str(v) for k, v in gap.parent_key.items())
    ]
    rejected_rows = len(incoming_rows) - len(scoped_rows)

    if fail:
        partition.status = PARTITION_FAILED
        partition.last_error = failure_reason or "立方构建失败"
        gap.status = GAP_STATUS_FAILED
        gap.history.append({
            "event": "构建重试失败",
            "attempt": partition.attempt,
            "rejected_adjacent_rows": rejected_rows,
            "reason": partition.last_error,
        })
        return gap

    partition.status = PARTITION_ONLINE
    partition.last_error = ""
    added: dict[str, float] = {}
    for name in gap.measure_names:
        values = [
            float(row.get(name, 0.0) or 0.0)
            for row in scoped_rows if row.get(name) is not None
        ]
        added[name] = round(sum(values), 2)
        partition.classified_measures[name] = round(
            float(partition.classified_measures.get(name, 0.0)) + added[name], 2
        )

    for name in gap.measure_names:
        gap.classified_measures[name] = round(
            float(gap.classified_measures.get(name, 0.0)) + added[name], 2
        )
        gap.pending_measures[name] = round(
            float(gap.parent_measures.get(name, 0.0)) - gap.classified_measures[name], 2
        )

    if all(abs(v) < 0.01 for v in gap.pending_measures.values()):
        gap.status = GAP_STATUS_CLASSIFIED
    else:
        gap.status = GAP_STATUS_PENDING
    gap.history.append({
        "event": "分区构建重试",
        "attempt": partition.attempt,
        "accepted_rows": len(scoped_rows),
        "rejected_adjacent_rows": rejected_rows,
        "added_measures": dict(added),
        "partition_status": partition.status,
    })
    return gap


def drill_gap_detail(
    gap: DrillGapRecord,
    partitions: list[CubePartition],
) -> dict[str, Any]:
    """下钻详情页数据：缺层记录关联立方分区，按父级标识读出已得事实与待回补。"""
    partition = find_partition(partitions, gap.parent_key, gap.level_dimension)
    return {
        "gap": gap.to_dict(),
        "partition": partition.to_dict() if partition is not None else None,
        "obtained_facts": {
            "parent_measures": dict(gap.parent_measures),
            "classified_measures": dict(gap.classified_measures),
        },
        "pending_backfill": {
            "measures": dict(gap.pending_measures),
            "reason_code": gap.reason_code,
            "reason_label": gap.reason_label,
        },
        "reconciliation": gap.reconciliation(),
        "balanced": gap.is_balanced(),
    }


# ---------------------------------------------------------------------------
# 查询加速预计算策略
# ---------------------------------------------------------------------------

def recommend_materialization(
    query_patterns: list[dict[str, Any]],  # [{query_id, frequency, avg_latency_ms, row_count}]
) -> list[dict[str, Any]]:
    """根据查询模式推荐物化策略。"""
    recommendations: list[dict[str, Any]] = []
    for q in query_patterns:
        freq = int(q.get("frequency", 0))
        latency = float(q.get("avg_latency_ms", 0))
        rows = int(q.get("row_count", 0))

        score = freq * max(1, latency / 100) * max(1, rows / 1000)
        if score > 50:
            method = "物化视图" if rows > 10000 else "预计算" if rows > 1000 else "结果缓存"
            strategy = {
                "query_id": q.get("query_id", ""),
                "score": round(score, 2),
                "recommended_method": method,
                "priority": "高" if score > 500 else "中" if score > 200 else "低",
            }
            recommendations.append(strategy)
    return sorted(recommendations, key=lambda x: float(x["score"]), reverse=True)


# ---------------------------------------------------------------------------
# 导出任务调度器
# ---------------------------------------------------------------------------

class ExportScheduler:
    """异步导出任务调度器（基于优先级队列）。"""

    def __init__(self, max_concurrent: int = 3):
        self._queue: list[dict[str, Any]] = []
        self._running: set[str] = set()
        self.max_concurrent = max_concurrent

    def enqueue(
        self, task_id: str, priority: int = 0,
        sql: str = "", format: str = "csv",
    ) -> str:
        self._queue.append({
            "task_id": task_id,
            "priority": priority,
            "sql": sql,
            "format": format,
            "enqueued_at": time.time(),
        })
        self._queue.sort(key=lambda x: (-x["priority"], x["enqueued_at"]))
        return task_id

    def dequeue(self) -> dict[str, Any] | None:
        if not self._queue or len(self._running) >= self.max_concurrent:
            return None
        task = self._queue.pop(0)
        self._running.add(task["task_id"])
        return task

    def complete(self, task_id: str) -> None:
        self._running.discard(task_id)

    def queue_size(self) -> int:
        return len(self._queue)

    def running_count(self) -> int:
        return len(self._running)