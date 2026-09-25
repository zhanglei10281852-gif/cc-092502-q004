"""植物遗存统计：密度、出现率与带自助重采样置信区间的分组比较。

所有随机性来自调用方注入的 random.Random 实例（由固定种子构造），
算法以 ALGORITHM_VERSION 标识；种子、迭代次数与版本必须随统计版本保存。

计数数据支持三类删失：
- exact   精确计数
- below   仅知上限 "<n"（最多 n-1，保守按 0 参与密度，同时给出上限口径）
- nd      未检出，检测限为 limit（真零点，仅在检出率口径中使用）

污染样品（现代根系侵入）默认从定量统计中剔除，但可通过 include_polluted 纳入。
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterable, Sequence

ALGORITHM_VERSION = "botany-bootstrap-1.0.0"

# 分组两两比较时每组最少样品数
MIN_GROUP_SIZE = 2


@dataclass(frozen=True)
class Count:
    """单条计数记录（单位为粒/枚）。"""

    count: float
    censor: str  # exact | below | nd
    limit: float | None = None

    def __post_init__(self) -> None:
        if self.censor not in {"exact", "below", "nd"}:
            raise ValueError(f"未知删失类型: {self.censor}")
        if self.censor == "exact" and self.count < 0:
            raise ValueError("计数不能为负")

    @property
    def detected(self) -> bool:
        """是否构成一次"检出"（出现率口径）。

        below（仅知上限 "<n"，n>=1）意味着至少有遗存但无法精确定数，记为检出；
        nd（低于检测限的未检出）记为未检出。
        """
        if self.censor == "below":
            return True  # "<n"（n>=1）表示确有遗存但无法精确定数
        return self.censor == "exact" and self.count > 0


@dataclass(frozen=True)
class Sample:
    sample_key: str
    context_type: str  # paleochannel(古河道) | dwelling(居址) | pit(灰坑)
    volume_liters: float
    polluted: bool
    counts: tuple[Count, ...]


def _quantile(sorted_values: Sequence[float], prob: float) -> float:
    """线性插值分位数（与 numpy 默认 'linear'、R type-7 一致）。"""
    if not sorted_values:
        raise ValueError("空样本无法计算分位数")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = prob * (len(sorted_values) - 1)
    low = int(pos)
    high = min(low + 1, len(sorted_values) - 1)
    frac = pos - low
    return sorted_values[low] * (1 - frac) + sorted_values[high] * frac


def _sample_density(sample: Sample) -> float:
    """每升密度（点估计口径）：精确计数之和 / 体积；below 保守记 0。"""
    exact = sum(c.count for c in sample.counts if c.censor == "exact")
    return exact / sample.volume_liters


def _sample_density_upper(sample: Sample) -> float:
    """上限口径密度：exact 按实际值，below("<n") 按最多 n-1 粒计。"""
    total = 0.0
    for c in sample.counts:
        if c.censor == "exact":
            total += c.count
        elif c.censor == "below":
            total += max(0.0, c.count - 1)
    return total / sample.volume_liters


def _sample_detected(sample: Sample) -> int:
    return 1 if any(c.detected for c in sample.counts) else 0


def _eligible(samples: Iterable[Sample], include_polluted: bool) -> list[Sample]:
    """零体积样品不参与任何密度统计（导入阶段本应拒绝，此处双保险）。"""
    return [s for s in samples if s.volume_liters > 0 and (include_polluted or not s.polluted)]


def summarize_group(samples: list[Sample], *, include_polluted: bool = False) -> dict:
    eligible = _eligible(samples, include_polluted)
    densities = [_sample_density(s) for s in eligible]
    upper = [_sample_density_upper(s) for s in eligible]
    detections = [_sample_detected(s) for s in eligible]
    n = len(eligible)
    total_volume = sum(s.volume_liters for s in eligible)
    total_exact = sum(c.count for s in eligible for c in s.counts if c.censor == "exact")
    return {
        "sample_count": n,
        "excluded_zero_volume": sum(1 for s in samples if s.volume_liters <= 0),
        "excluded_polluted": sum(1 for s in samples if s.polluted and s.volume_liters > 0) if not include_polluted else 0,
        "total_volume": round(total_volume, 9),
        # 合并口径密度：组内精确计数总和 / 组内体积总和
        "density_per_liter": round(total_exact / total_volume, 9) if total_volume > 0 else None,
        # 平均样品密度口径：各样品密度的算术平均（自助比较使用此口径）
        "density_mean": round(sum(densities) / n, 9) if n else None,
        "density_upper_mean": round(sum(upper) / n, 9) if n else None,
        "detection_rate": round(sum(detections) / n, 9) if n else None,
        "detected_samples": sum(detections),
    }


def _bootstrap_mean_ci(values: Sequence[float], rng: random.Random, iterations: int, confidence: float) -> list[float] | None:
    if not values:
        return None
    means: list[float] = []
    n = len(values)
    for _ in range(iterations):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    alpha = 1 - confidence
    lo = _quantile(means, alpha / 2)
    hi = _quantile(means, 1 - alpha / 2)
    return [round(lo, 9), round(hi, 9)]


def _bootstrap_rate_ci(flags: Sequence[int], rng: random.Random, iterations: int, confidence: float) -> list[float] | None:
    if not flags:
        return None
    rates: list[float] = []
    n = len(flags)
    for _ in range(iterations):
        rates.append(sum(flags[rng.randrange(n)] for _ in range(n)) / n)
    rates.sort()
    alpha = 1 - confidence
    return [round(_quantile(rates, alpha / 2), 9), round(_quantile(rates, 1 - alpha / 2), 9)]


def compare_groups(
    samples: Sequence[Sample],
    *,
    seed: int,
    iterations: int = 2000,
    confidence: float = 0.95,
    include_polluted: bool = False,
) -> dict:
    """按 context_type 分组比较密度与出现率，返回带自助置信区间的结果。

    随机数生成器只在本函数内由 seed 构造，保证同参数结果逐位可复算。
    """
    if iterations < 50:
        raise ValueError("自助迭代次数过少（至少 50）")
    if not 0.8 <= confidence < 1:
        raise ValueError("置信水平应落在 [0.8, 1)")

    eligible = _eligible(samples, include_polluted)
    groups: dict[str, list[Sample]] = {}
    for s in eligible:
        groups.setdefault(s.context_type, []).append(s)

    # 每组一个独立派生子流，避免分组字典顺序/组大小变化扰动其他组结果。
    master = random.Random(seed)
    group_seeds = {name: master.randrange(1, 2**31) for name in sorted(groups)}

    summaries: dict[str, dict] = {}
    for name in sorted(groups):
        members = groups[name]
        rng = random.Random(group_seeds[name])
        densities = [_sample_density(s) for s in members]
        flags = [_sample_detected(s) for s in members]
        summary = summarize_group(members, include_polluted=True)
        summary["density_ci"] = _bootstrap_mean_ci(densities, rng, iterations, confidence)
        summary["detection_rate_ci"] = _bootstrap_rate_ci(flags, random.Random(group_seeds[name] ^ 0x9E3779B9), iterations, confidence)
        summaries[name] = summary

    # 两两比较：密度均值差与出现率差，自助成对重采样（按组独立抽样）。
    contrasts = []
    names = sorted(groups)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            ga, gb = groups[a], groups[b]
            if len(ga) < MIN_GROUP_SIZE or len(gb) < MIN_GROUP_SIZE:
                contrasts.append({"group_a": a, "group_b": b, "comparable": False, "reason": "group_too_small"})
                continue
            da = [_sample_density(s) for s in ga]
            db = [_sample_density(s) for s in gb]
            fa = [_sample_detected(s) for s in ga]
            fb = [_sample_detected(s) for s in gb]
            rng = random.Random((group_seeds[a] * 73856093) ^ group_seeds[b] ^ 0x517CC1B7)
            diffs_d: list[float] = []
            diffs_r: list[float] = []
            for _ in range(iterations):
                ma = sum(da[rng.randrange(len(da))] for _ in range(len(da))) / len(da)
                mb = sum(db[rng.randrange(len(db))] for _ in range(len(db))) / len(db)
                ra = sum(fa[rng.randrange(len(fa))] for _ in range(len(fa))) / len(fa)
                rb = sum(fb[rng.randrange(len(fb))] for _ in range(len(fb))) / len(fb)
                diffs_d.append(ma - mb)
                diffs_r.append(ra - rb)
            diffs_d.sort()
            diffs_r.sort()
            alpha = 1 - confidence
            ci_d = [round(_quantile(diffs_d, alpha / 2), 9), round(_quantile(diffs_d, 1 - alpha / 2), 9)]
            ci_r = [round(_quantile(diffs_r, alpha / 2), 9), round(_quantile(diffs_r, 1 - alpha / 2), 9)]
            contrasts.append({
                "group_a": a,
                "group_b": b,
                "comparable": True,
                "density_diff": round((sum(da) / len(da)) - (sum(db) / len(db)), 9),
                "density_diff_ci": ci_d,
                "density_diff_ci_covers_zero": ci_d[0] <= 0 <= ci_d[1],
                "detection_rate_diff": round(sum(fa) / len(fa) - sum(fb) / len(fb), 9),
                "detection_rate_diff_ci": ci_r,
                "detection_rate_diff_ci_covers_zero": ci_r[0] <= 0 <= ci_r[1],
            })

    return {
        "algorithm_version": ALGORITHM_VERSION,
        "seed": seed,
        "iterations": iterations,
        "confidence": confidence,
        "include_polluted": include_polluted,
        "groups": summaries,
        "contrasts": contrasts,
    }
