"""植物遗存统计引擎。

按可选择的分类口径（family/genus/species）计算：
- 每升密度：组内样品计数合计 / 土样体积合计（ratio of sums）
- 出现率（ubiquity）：组内检出该分类的样品比例
- 分组比较：两两遗迹类型的密度差

仅知上限的计数按区间 [0, 上限] 处理，密度同时给出下限、上限与
中点点估计；置信区间对点估计做样品级自助重采样（percentile法）。
随机种子、迭代次数与算法版本随统计版本一并保存，保证可复算。
"""
from __future__ import annotations

import random
from typing import Any, Iterable

ALGORITHM_VERSION = "paleo-stats/1.0.0"

RANKS = ("family", "genus", "species")
CONFIDENCE_ORDER = {"": 0, "high": 3, "medium": 2, "low": 1}

DEFAULT_SEED = 42
DEFAULT_ITERATIONS = 1000
MAX_ITERATIONS = 100_000
MIN_ITERATIONS = 100


def taxon_key(row: dict[str, Any], rank: str) -> str:
    """按选定口径取分组键；缺失时沿 family→genus→species 回退，最终回退到原始名称。"""
    if row["is_unknown"]:
        return "unidentified"
    for field in (rank, "family", "genus", "species"):
        value = row.get(field) or ""
        if value.strip():
            return " ".join(value.strip().split()).casefold()
    return row["taxon_normalized"]


def build_taxon_map(ops: Iterable[dict[str, Any]]) -> dict[str, str]:
    """把已批准变更集的操作展开为 更名映射，并做链式归并（a→b、b→c 得 a→c）。"""
    mapping: dict[str, str] = {}
    for op in ops:
        target = op["to_taxon"]
        for source in op["from_taxa"]:
            if source != target:
                mapping[source] = target
    for _ in range(10):
        changed = False
        for source, target in list(mapping.items()):
            final = target
            seen = {source}
            while final in mapping and final not in seen:
                seen.add(final)
                final = mapping[final]
            if final != target and final != source:
                mapping[source] = final
                changed = True
        if not changed:
            break
    return mapping


def _count_bounds(row: dict[str, Any]) -> tuple[float | None, float | None]:
    """每行的计数区间；present 型无计数信息，返回 (None, None)。"""
    kind = row["count_type"]
    if kind == "exact":
        return row["count_value"], row["count_max"]
    if kind == "upper":
        return 0.0, row["count_max"]
    if kind == "absent":
        return 0.0, 0.0
    return None, None


def _is_presence(row: dict[str, Any]) -> bool:
    kind = row["count_type"]
    if kind == "present":
        return True
    if kind == "upper":
        return (row["count_max"] or 0) > 0
    if kind == "exact":
        return (row["count_value"] or 0) > 0
    return False


def _percentile(sorted_values: list[float], fraction: float) -> float:
    index = int(fraction * len(sorted_values))
    index = min(len(sorted_values) - 1, max(0, index))
    return sorted_values[index]


def _bootstrap(values: list[Any], statistic, iterations: int, rng: random.Random) -> tuple[float | None, float | None]:
    """对样品单元做有放回重抽样，返回 95% percentile 区间。统计量无法计算的重抽样本被丢弃。"""
    n = len(values)
    if n == 0:
        return None, None
    estimates: list[float] = []
    for _ in range(iterations):
        resample = [values[rng.randrange(n)] for _ in range(n)]
        value = statistic(resample)
        if value is not None:
            estimates.append(value)
    if not estimates:
        return None, None
    estimates.sort()
    return _percentile(estimates, 0.025), _percentile(estimates, 0.975)


def _density_stat(units: list[dict[str, float]]) -> float | None:
    volume = sum(unit["volume"] for unit in units)
    if volume <= 0:
        return None
    return sum(unit["mid"] for unit in units) / volume


def _ubiquity_stat(units: list[dict[str, Any]]) -> float | None:
    if not units:
        return None
    return sum(1 for unit in units if unit["present"]) / len(units)


def filter_rows(rows: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    """应用污染与置信度过滤（默认排除现代根系等污染行）。"""
    include_contaminated = bool(params.get("include_contaminated", False))
    min_confidence = params.get("min_confidence") or ""
    threshold = CONFIDENCE_ORDER.get(min_confidence, 0)
    kept = []
    for row in rows:
        if row["contamination"] and not include_contaminated:
            continue
        if CONFIDENCE_ORDER.get(row["confidence_level"], 0) < threshold:
            continue
        kept.append(row)
    return kept


def compute_statistics(
    samples: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    rank: str,
    seed: int,
    iterations: int,
    params: dict[str, Any],
    taxon_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """核心统计函数，纯计算、不写库，保证同参数同数据结果可复现。

    samples: [{sample_id, sample_code, context_type, volume_liters}]（体积为该样品全部批次合计）
    rows: 鉴定行（已关联样品），字段见 service 的查询。
    """
    if rank not in RANKS:
        raise ValueError(f"不支持的分类口径: {rank}")
    mapping = taxon_map or {}
    rng = random.Random(seed)

    kept = filter_rows(rows, params)

    sample_info = {s["sample_id"]: s for s in samples}
    groups: dict[str, list[int]] = {}
    for s in samples:
        groups.setdefault(s["context_type"], []).append(s["sample_id"])

    # (group, taxon) -> {sample_id: {min, max, present}}
    cells: dict[tuple[str, str], dict[int, dict[str, Any]]] = {}
    for row in kept:
        sample = sample_info.get(row["sample_id"])
        if sample is None:
            continue
        group = sample["context_type"]
        key = taxon_key(row, rank)
        key = mapping.get(key, key)
        cell = cells.setdefault((group, key), {})
        entry = cell.setdefault(row["sample_id"], {"min": 0.0, "max": 0.0, "present": False})
        low, high = _count_bounds(row)
        if low is not None:
            entry["min"] += low
            entry["max"] += high
        if _is_presence(row):
            entry["present"] = True

    taxa = sorted({key for _, key in cells})
    results: list[dict[str, Any]] = []
    group_summaries: list[dict[str, Any]] = []
    density_units: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for group in sorted(groups):
        member_ids = sorted(groups[group])
        total_volume = sum(sample_info[sid]["volume_liters"] for sid in member_ids)
        group_summaries.append({
            "group_key": group,
            "n_samples": len(member_ids),
            "total_volume_liters": total_volume,
            "min_detectable_density": (1.0 / total_volume) if total_volume > 0 else None,
        })
        for taxon in taxa:
            cell = cells.get((group, taxon), {})
            units = []
            for sid in member_ids:
                entry = cell.get(sid, {"min": 0.0, "max": 0.0, "present": False})
                units.append({
                    "volume": sample_info[sid]["volume_liters"],
                    "min": entry["min"],
                    "max": entry["max"],
                    "mid": (entry["min"] + entry["max"]) / 2,
                    "present": entry["present"],
                })
            # 零体积样品不参与密度（分子分母均排除），但仍计入出现率
            dunits = [u for u in units if u["volume"] > 0]
            density_volume = sum(u["volume"] for u in dunits)
            count_min = sum(u["min"] for u in dunits)
            count_max = sum(u["max"] for u in dunits)
            n_present = sum(1 for u in units if u["present"])
            if density_volume > 0:
                density_min = count_min / density_volume
                density_max = count_max / density_volume
                density_point = sum(u["mid"] for u in dunits) / density_volume
            else:
                density_min = density_max = density_point = None
            density_ci = _bootstrap(dunits, _density_stat, iterations, rng)
            ubiquity = n_present / len(member_ids) if member_ids else None
            ubiquity_ci = _bootstrap(units, _ubiquity_stat, iterations, rng)
            density_units[(group, taxon)] = dunits
            results.append({
                "group_key": group,
                "taxon_key": taxon,
                "n_samples": len(member_ids),
                "n_present": n_present,
                "total_volume_liters": total_volume,
                "count_min": count_min,
                "count_max": count_max,
                "density_min": density_min,
                "density_max": density_max,
                "density_point": density_point,
                "density_ci_low": density_ci[0],
                "density_ci_high": density_ci[1],
                "ubiquity": ubiquity,
                "ubiquity_ci_low": ubiquity_ci[0],
                "ubiquity_ci_high": ubiquity_ci[1],
            })

    comparisons: list[dict[str, Any]] = []
    group_keys = sorted(groups)
    for taxon in taxa:
        present_groups = [g for g in group_keys if any(u["present"] or u["max"] > 0 for u in density_units[(g, taxon)])]
        for i, group_a in enumerate(present_groups):
            for group_b in present_groups[i + 1:]:
                units_a = density_units[(group_a, taxon)]
                units_b = density_units[(group_b, taxon)]
                point_a, point_b = _density_stat(units_a), _density_stat(units_b)
                diff_point = None if point_a is None or point_b is None else point_a - point_b
                estimates: list[float] = []
                for _ in range(iterations):
                    resample_a = [units_a[rng.randrange(len(units_a))] for _ in range(len(units_a))]
                    resample_b = [units_b[rng.randrange(len(units_b))] for _ in range(len(units_b))]
                    va, vb = _density_stat(resample_a), _density_stat(resample_b)
                    if va is not None and vb is not None:
                        estimates.append(va - vb)
                if estimates:
                    estimates.sort()
                    ci_low, ci_high = _percentile(estimates, 0.025), _percentile(estimates, 0.975)
                else:
                    ci_low = ci_high = None
                comparisons.append({
                    "taxon_key": taxon,
                    "group_a": group_a,
                    "group_b": group_b,
                    "diff_point": diff_point,
                    "diff_ci_low": ci_low,
                    "diff_ci_high": ci_high,
                })

    return {
        "algorithm_version": ALGORITHM_VERSION,
        "groups": group_summaries,
        "results": results,
        "comparisons": comparisons,
    }
