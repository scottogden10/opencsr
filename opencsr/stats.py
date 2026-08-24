"""Validated deterministic calculators.

These are the only place numeric derivations are allowed to happen.
Agents must call these through the governed tool gateway; they never
compute statistics "in their head" for a claim.
"""

from __future__ import annotations

import math


def _binom_pmf(k: int, n: int, p: float) -> float:
    if p <= 0.0:
        return 1.0 if k == 0 else 0.0
    if p >= 1.0:
        return 1.0 if k == n else 0.0
    return math.comb(n, k) * (p**k) * ((1.0 - p) ** (n - k))


def _binom_cdf(k: int, n: int, p: float) -> float:
    return sum(_binom_pmf(i, n, p) for i in range(0, k + 1))


def _bisect(f, lo: float, hi: float, tol: float = 1e-12, iters: int = 200) -> float:
    flo = f(lo)
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        fm = f(mid)
        if abs(hi - lo) < tol:
            return mid
        if (flo < 0) == (fm < 0):
            lo, flo = mid, fm
        else:
            hi = mid
    return (lo + hi) / 2.0


def clopper_pearson(x: int, n: int, conf: float = 0.95) -> tuple[float, float]:
    """Exact (Clopper-Pearson) two-sided binomial confidence interval.

    Returns (lower, upper) as proportions in [0, 1]. Computed by inverting
    the exact binomial tail probabilities (equivalent to the beta-quantile
    formulation) via bisection — pure stdlib, no scipy.
    """
    if n <= 0:
        raise ValueError("n must be > 0")
    if not 0 <= x <= n:
        raise ValueError("x must be in [0, n]")
    alpha = 1.0 - conf
    if x == 0:
        lower = 0.0
    else:
        # largest p with P(X >= x | p) = alpha/2  <=>  1 - cdf(x-1) = alpha/2
        lower = _bisect(lambda p: (1.0 - _binom_cdf(x - 1, n, p)) - alpha / 2.0, 0.0, 1.0)
    if x == n:
        upper = 1.0
    else:
        # smallest p with P(X <= x | p) = alpha/2
        upper = _bisect(lambda p: _binom_cdf(x, n, p) - alpha / 2.0, 0.0, 1.0)
    return lower, upper


def pct(numerator: int, denominator: int, decimals: int = 1) -> float:
    """Percentage rounded to a fixed number of decimals (round-half-up)."""
    if denominator == 0:
        raise ValueError("denominator must be > 0")
    raw = 100.0 * numerator / denominator
    q = 10**decimals
    return math.floor(raw * q + 0.5) / q


def orr_with_ci(x: int, n: int, conf: float = 0.95, decimals: int = 1) -> dict:
    """Response rate with exact CI, formatted per CSR numeric conventions."""
    lo, hi = clopper_pearson(x, n, conf)
    q = 10**decimals
    return {
        "numerator": x,
        "denominator": n,
        "estimate_pct": pct(x, n, decimals),
        "ci_level": round(conf * 100),
        "ci_lower_pct": math.floor(lo * 100 * q + 0.5) / q,
        "ci_upper_pct": math.floor(hi * 100 * q + 0.5) / q,
        "method": "Clopper-Pearson exact",
    }
