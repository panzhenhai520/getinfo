#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""趋势爆发检测 + 五态状态机（纯 numpy，无第三方依赖）。

输入：一个关键词/主题的"每日文章数"时间序列（按天桶，越新越靠后）。
输出：爆发分、是否爆发、趋势状态（EMERGING/RISING/BURSTING/MATURE/DECLINING）。

第一阶段 baseline 算法：移动平均 + 2σ。第三阶段语义漂移、Kleinberg
多级爆发在此之上扩展。所有阈值集中为模块常量，便于按行业调参。
"""

from __future__ import annotations

import math
from typing import Dict, List, Sequence

import numpy as np

# ---- 趋势状态 ----
EMERGING = "EMERGING"        # 新生 / 数据不足
RISING = "RISING"            # 持续上升
BURSTING = "BURSTING"        # 爆发
MATURE = "MATURE"            # 高位平稳
DECLINING = "DECLINING"      # 衰退

# ---- 爆发检测阈值 ----
BURST_SIGMA = 2.0            # baseline 有波动时：阈值 = mean + BURST_SIGMA * std
BURST_STD_FLOOR = 0.5        # baseline 标准差低于此值视为近恒定，改用绝对/倍数判定
BURST_RATIO = 1.5            # 波动场景：recent 还需 >= baseline * BURST_RATIO
BURST_RATIO_FLAT = 2.0       # 恒定场景：recent 需 >= baseline * BURST_RATIO_FLAT
BURST_MIN_RECENT_ABS = 3.0   # 恒定场景：recent 均值绝对下限（过滤 0→1 噪声）
BURST_MIN_ABS_DELTA = 2.0    # 恒定场景：recent - baseline 绝对增量下限

# ---- 状态机阈值 ----
DECLINE_BASELINE_MIN = 0.5   # baseline 低于此值不判衰退（本就没什么量）
DECLINE_RATIO = 0.5          # recent < baseline * DECLINE_RATIO → 衰退
RISE_RATIO = 1.3             # slope>0 且 recent > baseline * RISE_RATIO → 上升
MATURE_RATIO = 0.7           # recent >= baseline * MATURE_RATIO → 高位
MATURE_MIN_RECENT = 1.0      # MATURE 还需近窗日均 >= 此值（过滤全 0 / 无活动）
SLOPE_FLAT_RATIO = 0.1       # |slope| < baseline * 此比例 → 视为平稳


def _average_ranks(values: np.ndarray) -> np.ndarray:
    """Equivalent to pandas rank(method='average'), kept NumPy-only."""
    values = np.asarray(values, dtype=float)
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(len(values), dtype=float)
    start = 0
    while start < len(values):
        end = start + 1
        while end < len(values) and values[order[end]] == values[order[start]]:
            end += 1
        ranks[order[start:end]] = ((start + 1) + end) / 2.0
        start = end
    return ranks


def pettitt(values: Sequence[float]) -> tuple[int, float, float]:
    """TaskShift V2.1 Pettitt non-parametric change-point primitive."""
    arr = np.asarray(values, dtype=float)
    n = len(arr)
    if n < 2:
        return 1, 0.0, 1.0
    ranks = _average_ranks(arr)
    statistic = np.cumsum(2.0 * ranks - (n + 1.0))
    tau = int(np.argmax(np.abs(statistic))) + 1
    magnitude = float(np.max(np.abs(statistic)))
    p_value = min(1.0, 2.0 * math.exp(-6.0 * magnitude * magnitude / (n**3 + n**2)))
    return tau, magnitude, p_value


def _poisson_ll(values: np.ndarray, mean: float) -> float:
    mean = max(float(mean), 1e-9)
    return float(sum(-mean + value * math.log(mean) - math.lgamma(value + 1.0) for value in values))


def _poisson_delta_bic(values: np.ndarray, min_segment: int = 2) -> tuple[int | None, float]:
    n = len(values)
    if n < min_segment * 2 + 1 or float(values.sum()) <= 0:
        return None, -999.0
    single_ll = _poisson_ll(values, float(values.mean()))
    single_bic = -2.0 * single_ll + math.log(n)
    best = None
    for tau in range(min_segment, n - min_segment):
        left, right = values[:tau], values[tau:]
        if left.mean() <= 0 or right.mean() <= 0:
            continue
        ll = _poisson_ll(left, float(left.mean())) + _poisson_ll(right, float(right.mean()))
        bic = -2.0 * ll + 2.0 * math.log(n)
        if best is None or bic < best[1]:
            best = (tau, bic)
    return (best[0], single_bic - best[1]) if best else (None, -999.0)


def _binomial_ll(successes: np.ndarray, totals: np.ndarray, probability: float) -> float:
    probability = min(max(float(probability), 1e-9), 1.0 - 1e-9)
    return float(sum(
        math.lgamma(total + 1.0) - math.lgamma(success + 1.0) - math.lgamma(total - success + 1.0)
        + success * math.log(probability) + (total - success) * math.log(1.0 - probability)
        for success, total in zip(successes, totals)
    ))


def _binomial_delta_bic(successes: np.ndarray, totals: np.ndarray, min_segment: int = 2) -> tuple[int | None, float]:
    length = len(successes)
    if length < min_segment * 2 + 1 or totals.sum() <= 0 or successes.sum() <= 0:
        return None, -999.0
    p0 = successes.sum() / totals.sum()
    ll0 = _binomial_ll(successes, totals, p0)
    bic0 = -2.0 * ll0 + math.log(length)
    best = None
    for tau in range(min_segment, length - min_segment):
        left_total, right_total = totals[:tau].sum(), totals[tau:].sum()
        if left_total <= 0 or right_total <= 0:
            continue
        ll = _binomial_ll(successes[:tau], totals[:tau], successes[:tau].sum() / left_total)
        ll += _binomial_ll(successes[tau:], totals[tau:], successes[tau:].sum() / right_total)
        bic = -2.0 * ll + 2.0 * math.log(length)
        if best is None or bic < best[1]:
            best = (tau, bic)
    return (best[0], bic0 - best[1]) if best else (None, -999.0)


def taskshift_statistics(
    series: Sequence[float],
    *,
    exposures: Sequence[float] | None = None,
    min_series: int = 8,
    min_post: int = 3,
    alpha: float = 0.05,
) -> Dict:
    """Adapt TaskShift Pettitt+BIC to industry-article time series.

    When daily corpus totals are supplied, the test follows V2.1's adoption-rate
    route (N/M + binomial BIC). Otherwise it uses V2's count + Poisson BIC route.
    """
    counts = np.asarray(series, dtype=float)
    tested = counts
    method = "count_poisson"
    if exposures is not None:
        totals = np.asarray(exposures, dtype=float)
        if len(totals) != len(counts) or np.any(counts < 0) or np.any(totals < counts):
            raise ValueError("trend exposures must align with series and be >= counts")
        tested = np.divide(counts, totals, out=np.zeros_like(counts), where=totals > 0)
        _bic_tau, delta_bic = _binomial_delta_bic(counts, totals)
        method = "adoption_binomial"
    else:
        _bic_tau, delta_bic = _poisson_delta_bic(counts)
    default = {
        "trend_test_method": method, "pettitt_p": 1.0, "pettitt_q": 1.0,
        "delta_BIC": round(float(delta_bic), 2), "change_point_index": None,
        "pre_mean": 0.0, "post_mean": 0.0, "cond_break": False,
        "cond_direction": False, "taskshift_emerging": False,
        "taskshift_high_confidence": False,
    }
    if len(counts) < min_series or counts.sum() <= 0:
        return default
    tau, _magnitude, p_value = pettitt(tested)
    pre, post = tested[:tau], tested[tau:]
    pre_mean = float(pre.mean()) if len(pre) else 0.0
    post_mean = float(post.mean()) if len(post) else 0.0
    cond_break = len(post) >= min_post and p_value < alpha and delta_bic > 0
    cond_direction = post_mean > pre_mean
    default.update({
        "pettitt_p": round(p_value, 6), "pettitt_q": round(p_value, 6),
        "change_point_index": tau - 1, "pre_mean": round(pre_mean, 6),
        "post_mean": round(post_mean, 6), "cond_break": bool(cond_break),
        "cond_direction": bool(cond_direction),
        "taskshift_emerging": bool(cond_break and cond_direction),
    })
    return default


def benjamini_hochberg(p_values: Sequence[float]) -> np.ndarray:
    p_values = np.asarray(p_values, dtype=float)
    count = len(p_values)
    if not count:
        return np.asarray([], dtype=float)
    order = np.argsort(p_values)
    adjusted = np.empty(count, dtype=float)
    running = 1.0
    for index in range(count - 1, -1, -1):
        running = min(running, p_values[order[index]] * count / (index + 1))
        adjusted[order[index]] = min(1.0, running)
    return adjusted


def apply_bh_correction(items: List[Dict], q_threshold: float = 0.10) -> List[Dict]:
    """BH-correct the complete family of trend tests in one API result."""
    if not items:
        return items
    q_values = benjamini_hochberg([float(item.get("pettitt_p", 1.0)) for item in items])
    for item, q_value in zip(items, q_values):
        item["pettitt_q"] = round(float(q_value), 6)
        item["taskshift_high_confidence"] = bool(
            item.get("taskshift_emerging") and q_value <= q_threshold
        )
    return items


def _split_window(arr: np.ndarray, window: int):
    """切分 baseline（历史）与 recent（近窗）两段。

    - n >= 2w：baseline = arr[-2w:-w]，recent = arr[-w:]
    - w <= n < 2w：baseline = arr[:n-w]，recent = arr[n-w:]（recent 满 w 个）
    - 2 <= n < w：前后对半
    - n < 2：退化，两段都取现有
    """
    n = len(arr)
    if n >= 2 * window:
        return arr[-2 * window : -window], arr[-window:]
    if n >= window:
        return arr[: n - window], arr[n - window :]
    if n >= 2:
        mid = n // 2
        return arr[:mid], arr[mid:]
    return arr, arr


def _decide_burst(baseline_mean: float, baseline_std: float, recent_mean: float) -> bool:
    """是否爆发。baseline 有波动用 2σ；近恒定改用倍数 + 绝对增量。"""
    if baseline_std >= BURST_STD_FLOOR:
        threshold = baseline_mean + BURST_SIGMA * baseline_std
        return recent_mean >= threshold and recent_mean >= baseline_mean * BURST_RATIO
    # baseline 近恒定：要求翻倍且有绝对量级，避免 0→1 噪声误报
    return (
        recent_mean >= max(baseline_mean * BURST_RATIO_FLAT, BURST_MIN_RECENT_ABS)
        and (recent_mean - baseline_mean) >= BURST_MIN_ABS_DELTA
    )


def _decide_state(
    n: int,
    window: int,
    is_burst: bool,
    baseline_mean: float,
    recent_mean: float,
    slope: float,
) -> str:
    """五态优先级判定。"""
    if n < window:
        return EMERGING  # 数据不足，强制新生，不误报爆发
    if is_burst:
        return BURSTING
    if baseline_mean > DECLINE_BASELINE_MIN and recent_mean < baseline_mean * DECLINE_RATIO:
        return DECLINING
    if slope > 0 and recent_mean > baseline_mean * RISE_RATIO:
        return RISING
    if (
        recent_mean >= baseline_mean * MATURE_RATIO
        and recent_mean >= MATURE_MIN_RECENT
        and abs(slope) < SLOPE_FLAT_RATIO * max(baseline_mean, 1.0)
    ):
        return MATURE
    return EMERGING


def analyze(series: Sequence[float], window: int = 7, *, exposures: Sequence[float] | None = None) -> Dict:
    """一次计算全部趋势指标。series 为每日文章数（越新越靠后）。"""
    arr = np.asarray(series, dtype=float)
    n = int(len(arr))
    baseline_arr, recent_arr = _split_window(arr, window)
    baseline_mean = float(np.mean(baseline_arr)) if len(baseline_arr) else 0.0
    baseline_std = float(np.std(baseline_arr)) if len(baseline_arr) else 0.0
    recent_mean = (
        float(np.mean(recent_arr)) if len(recent_arr) else (float(arr[-1]) if n else 0.0)
    )
    # 近窗线性斜率（捕捉持续上升/下降）
    if len(recent_arr) >= 2:
        x = np.arange(len(recent_arr), dtype=float)
        slope = float(np.polyfit(x, recent_arr, 1)[0])
    else:
        slope = 0.0
    # 爆发分：平滑 z（分母 +1.0 避免 std=0 爆炸，且让小量级可比）
    burst_score = (recent_mean - baseline_mean) / (baseline_std + 1.0)
    is_burst = _decide_burst(baseline_mean, baseline_std, recent_mean)
    state = _decide_state(n, window, is_burst, baseline_mean, recent_mean, slope)
    result = {
        "state": state,
        "is_burst": is_burst,
        "burst_score": round(burst_score, 4),
        "recent_avg": round(recent_mean, 4),
        "baseline_avg": round(baseline_mean, 4),
        "slope": round(slope, 4),
        "n": n,
    }
    result.update(taskshift_statistics(series, exposures=exposures))
    return result


def detect_burst(series: Sequence[float], window: int = 7) -> Dict:
    """爆发检测，返回 {is_burst, burst_score, recent_avg, baseline_avg}。"""
    r = analyze(series, window)
    return {
        "is_burst": r["is_burst"],
        "burst_score": r["burst_score"],
        "recent_avg": r["recent_avg"],
        "baseline_avg": r["baseline_avg"],
    }


def compute_state(series: Sequence[float], window: int = 7) -> str:
    """趋势状态（五态之一）。"""
    return analyze(series, window)["state"]
