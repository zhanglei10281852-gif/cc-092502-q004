"""统计纯函数层测试：固定种子确定性、零体积排除、删失与污染口径。"""
from __future__ import annotations

import random

import pytest

from app.botany import stats


def s(key, ctype, volume, counts, polluted=False):
    return stats.Sample(key, ctype, volume, polluted, tuple(counts))


def test_fixed_seed_byte_identical():
    samples = [
        s("a", "pit", 4, [stats.Count(3, "exact")]),
        s("b", "pit", 6, [stats.Count(7, "exact")]),
        s("c", "dwelling", 5, [stats.Count(1, "exact")]),
        s("d", "dwelling", 5, [stats.Count(9, "exact")]),
    ]
    r1 = stats.compare_groups(samples, seed=12345, iterations=500)
    r2 = stats.compare_groups(samples, seed=12345, iterations=500)
    r3 = stats.compare_groups(samples, seed=12345, iterations=501)
    assert r1 == r2
    assert r1 != r3
    # 元信息齐全，供复算
    assert r1["seed"] == 12345 and r1["iterations"] == 500
    assert r1["algorithm_version"] == "botany-bootstrap-1.0.0"
    contrast = r1["contrasts"][0]
    assert contrast["density_diff_ci"][0] <= contrast["density_diff"] <= contrast["density_diff_ci"][1]


def test_zero_volume_excluded():
    samples = [
        s("ok", "pit", 4, [stats.Count(8, "exact")]),
        s("zero", "pit", 0, [stats.Count(99, "exact")]),  # 零体积：不参与密度
    ]
    summary = stats.summarize_group(samples)
    assert summary["sample_count"] == 1
    assert summary["excluded_zero_volume"] == 1
    assert summary["density_per_liter"] == 2.0
    result = stats.compare_groups(samples, seed=1, iterations=100)
    assert result["groups"]["pit"]["sample_count"] == 1


def test_censor_semantics():
    # below <3：检出但点估计密度记 0，上限口径记 2；nd：未检出
    below = s("b", "pit", 2, [stats.Count(3, "below")])
    nd = s("n", "pit", 2, [stats.Count(0, "nd", 2.0)])
    exact = s("e", "pit", 2, [stats.Count(4, "exact")])
    group = [below, nd, exact]
    summary = stats.summarize_group(group)
    assert summary["density_mean"] == pytest.approx((0 + 0 + 2) / 3)
    assert summary["density_upper_mean"] == pytest.approx((1 + 0 + 2) / 3)
    assert summary["detection_rate"] == pytest.approx(2 / 3)
    assert stats.Count(3, "below").detected is True
    assert stats.Count(0, "nd").detected is False
    assert stats.Count(0, "exact").detected is False
    assert stats.Count(2, "exact").detected is True


def test_pollution_exclusion():
    clean = s("c", "pit", 4, [stats.Count(1, "exact")])
    dirty = s("d", "pit", 4, [stats.Count(100, "exact")], polluted=True)
    default_summary = stats.summarize_group([clean, dirty])
    assert default_summary["sample_count"] == 1
    assert default_summary["excluded_polluted"] == 1
    included = stats.summarize_group([clean, dirty], include_polluted=True)
    assert included["sample_count"] == 2


def test_group_too_small_not_comparable():
    samples = [s("a", "pit", 4, [stats.Count(1, "exact")]), s("b", "dwelling", 4, [stats.Count(2, "exact")])]
    result = stats.compare_groups(samples, seed=9, iterations=100)
    assert result["contrasts"][0]["comparable"] is False


def test_bad_arguments():
    samples = [s("a", "pit", 4, [stats.Count(1, "exact")])]
    with pytest.raises(ValueError):
        stats.compare_groups(samples, seed=1, iterations=10)
    with pytest.raises(ValueError):
        stats.compare_groups(samples, seed=1, iterations=100, confidence=0.5)
    with pytest.raises(ValueError):
        stats.Count(-1, "exact")
    with pytest.raises(ValueError):
        stats.Count(1, "weird")


def test_quantile_matches_type7():
    values = [0.0, 1.0, 2.0, 3.0, 4.0]
    assert stats._quantile(values, 0.5) == 2.0
    assert stats._quantile(values, 0.0) == 0.0
    assert stats._quantile(values, 1.0) == 4.0
    # 随机流稳定性：固定种子的抽样序列可预测
    rng = random.Random(7)
    assert [rng.randrange(10) for _ in range(5)] == [5, 2, 6, 0, 1]
