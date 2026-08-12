"""
Quantitative performance and risk metrics for mutual fund NAV series.

Every function in this module is pure: it takes a NAV/return series and returns
numbers. No I/O, no logging, no database access. That makes the whole analytics
surface unit-testable against closed-form known answers (see `tests/test_metrics.py`).

Conventions used throughout (documented because every downstream number inherits them):

* NAV series are indexed by ``DatetimeIndex``, ascending, one observation per
  trading day. Non-trading days are simply absent -- they are never forward filled,
  because filling would understate volatility.
* Daily returns are **simple** (arithmetic) returns: ``nav_t / nav_{t-1} - 1``.
* Annualisation uses ``TRADING_DAYS_PER_YEAR`` (252) for volatility-type
  statistics and calendar years (``days / 365.25``) for CAGR-type statistics.
  Mixing the two is standard practice: volatility scales with observation count,
  compound growth scales with wall-clock time.
* The risk-free rate is supplied as an **annual** decimal (0.065 = 6.5%) and
  converted to a daily rate geometrically: ``(1 + rf) ** (1 / 252) - 1``.
* Returns are expressed in **percent** when the name ends in ``_pct``, and as
  decimals otherwise. Ratios (Sharpe, Sortino, beta) are unitless.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252
DAYS_PER_YEAR = 365.25

# Minimum observation counts below which a statistic is not meaningful and we
# return None rather than a number that would look authoritative but be noise.
MIN_OBS_FOR_VOLATILITY = 20
MIN_OBS_FOR_REGRESSION = 60


# ---------------------------------------------------------------------------
# Series preparation
# ---------------------------------------------------------------------------


def to_nav_series(frame: pd.DataFrame, date_col: str = "date", nav_col: str = "nav") -> pd.Series:
    """Convert a (date, nav) frame into a clean, sorted, deduplicated NAV series.

    Duplicate dates keep the **last** observation, matching the database upsert
    semantics where a revised NAV supersedes the original.
    """
    series = (
        frame[[date_col, nav_col]]
        .dropna()
        .assign(**{date_col: lambda d: pd.to_datetime(d[date_col])})
        .drop_duplicates(subset=date_col, keep="last")
        .sort_values(date_col)
        .set_index(date_col)[nav_col]
        .astype(float)
    )
    series.index.name = "date"
    return series[series > 0]


def daily_returns(nav: pd.Series) -> pd.Series:
    """Simple daily returns. Length is ``len(nav) - 1``."""
    return nav.pct_change().dropna()


def monthly_returns(nav: pd.Series) -> pd.Series:
    """Month-end compounded returns, used for capture ratios and monthly tables."""
    month_end = nav.resample("ME").last().dropna()
    return month_end.pct_change().dropna()


def daily_risk_free(annual_rate: float) -> float:
    """Geometric daily equivalent of an annual risk-free rate."""
    return (1.0 + annual_rate) ** (1.0 / TRADING_DAYS_PER_YEAR) - 1.0


def nav_asof(nav: pd.Series, target: pd.Timestamp) -> float | None:
    """NAV on ``target``, or the most recent prior trading day.

    Returns ``None`` when the series starts after ``target`` -- the caller must
    treat that as "insufficient history", never as zero.
    """
    eligible = nav.loc[:target]
    if eligible.empty:
        return None
    return float(eligible.iloc[-1])


# ---------------------------------------------------------------------------
# Return metrics
# ---------------------------------------------------------------------------


def absolute_return_pct(start_nav: float, end_nav: float) -> float | None:
    """Point-to-point percentage growth."""
    if start_nav is None or end_nav is None or start_nav <= 0:
        return None
    return (end_nav / start_nav - 1.0) * 100.0


def cagr_pct(start_nav: float, end_nav: float, years: float) -> float | None:
    """Compound annual growth rate in percent.

    Undefined for horizons under one month (``years < 1/12``): annualising a
    handful of days produces explosive, meaningless numbers.
    """
    if start_nav is None or end_nav is None or start_nav <= 0 or end_nav <= 0:
        return None
    if years is None or years < 1.0 / 12.0:
        return None
    return ((end_nav / start_nav) ** (1.0 / years) - 1.0) * 100.0


def trailing_return_pct(nav: pd.Series, years: float) -> float | None:
    """Trailing return over ``years``: absolute below 1y, annualised (CAGR) at or above 1y.

    This mirrors SEBI/AMFI disclosure convention -- funds must not annualise
    sub-one-year performance.
    """
    if nav.empty:
        return None
    end_date = nav.index[-1]
    end_nav = float(nav.iloc[-1])
    target = end_date - pd.DateOffset(days=round(years * DAYS_PER_YEAR))
    start_nav = nav_asof(nav, target)
    if start_nav is None:
        return None
    # Guard against a "start" that is actually the end point (too little history).
    actual_years = (end_date - nav.loc[:target].index[-1]).days / DAYS_PER_YEAR
    if actual_years < years * 0.75:  # more than 25% of the window missing
        return None
    if years < 1.0:
        return absolute_return_pct(start_nav, end_nav)
    return cagr_pct(start_nav, end_nav, actual_years)


def since_inception_cagr_pct(nav: pd.Series) -> float | None:
    """CAGR across the full available history."""
    if len(nav) < 2:
        return None
    years = (nav.index[-1] - nav.index[0]).days / DAYS_PER_YEAR
    return cagr_pct(float(nav.iloc[0]), float(nav.iloc[-1]), years)


# ---------------------------------------------------------------------------
# Risk metrics
# ---------------------------------------------------------------------------


def annualised_volatility_pct(returns: pd.Series) -> float | None:
    """Annualised standard deviation of daily returns, in percent.

    Uses the sample standard deviation (``ddof=1``) -- the series is a sample of
    the return-generating process, not the population.
    """
    if len(returns) < MIN_OBS_FOR_VOLATILITY:
        return None
    return float(returns.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR) * 100.0)


def downside_deviation_pct(returns: pd.Series, mar_daily: float = 0.0) -> float | None:
    """Annualised deviation of returns *below* a minimum acceptable return (MAR).

    Shortfalls are squared and averaged over the **full** observation count (not
    just the losing days), which is the Sortino (1994) definition.
    """
    if len(returns) < MIN_OBS_FOR_VOLATILITY:
        return None
    shortfall = np.minimum(returns.to_numpy() - mar_daily, 0.0)
    dd = math.sqrt(float(np.mean(shortfall**2)))
    return dd * math.sqrt(TRADING_DAYS_PER_YEAR) * 100.0


def sharpe_ratio(returns: pd.Series, annual_risk_free: float) -> float | None:
    """Annualised Sharpe ratio: excess return per unit of total volatility."""
    if len(returns) < MIN_OBS_FOR_VOLATILITY:
        return None
    excess = returns - daily_risk_free(annual_risk_free)
    sigma = excess.std(ddof=1)
    if sigma == 0 or not np.isfinite(sigma):
        return None
    return float(excess.mean() / sigma * math.sqrt(TRADING_DAYS_PER_YEAR))


def sortino_ratio(returns: pd.Series, annual_risk_free: float) -> float | None:
    """Annualised Sortino ratio: excess return per unit of *downside* volatility."""
    if len(returns) < MIN_OBS_FOR_VOLATILITY:
        return None
    rf_daily = daily_risk_free(annual_risk_free)
    excess_annual = float(returns.mean() - rf_daily) * TRADING_DAYS_PER_YEAR
    dd = downside_deviation_pct(returns, mar_daily=rf_daily)
    if not dd:
        return None
    return excess_annual * 100.0 / dd


@dataclass(frozen=True)
class Drawdown:
    """A peak-to-trough decline and its recovery status."""

    depth_pct: float
    peak_date: date | None
    trough_date: date | None
    recovery_date: date | None
    drawdown_days: int | None
    recovered: bool

    @property
    def underwater_days(self) -> int | None:
        """Calendar days from peak to recovery (or to series end if still underwater)."""
        return self.drawdown_days


def max_drawdown(nav: pd.Series) -> Drawdown | None:
    """Worst peak-to-trough decline, with peak/trough/recovery dates.

    ``depth_pct`` is negative (a 20% fall is ``-20.0``).
    """
    if len(nav) < 2:
        return None
    running_peak = nav.cummax()
    drawdown = nav / running_peak - 1.0
    trough_date = drawdown.idxmin()
    depth = float(drawdown.loc[trough_date])
    if depth == 0.0:
        return Drawdown(0.0, None, None, None, 0, True)

    peak_date = nav.loc[:trough_date].idxmax()
    peak_value = float(nav.loc[peak_date])
    after = nav.loc[trough_date:]
    recovered_points = after[after >= peak_value]
    recovery_date = recovered_points.index[0] if len(recovered_points) else None
    end = recovery_date if recovery_date is not None else nav.index[-1]

    return Drawdown(
        depth_pct=depth * 100.0,
        peak_date=peak_date.date(),
        trough_date=trough_date.date(),
        recovery_date=recovery_date.date() if recovery_date is not None else None,
        drawdown_days=int((end - peak_date).days),
        recovered=recovery_date is not None,
    )


def calmar_ratio(cagr_percent: float | None, mdd_percent: float | None) -> float | None:
    """CAGR divided by the absolute worst drawdown -- return per unit of pain."""
    if cagr_percent is None or mdd_percent is None or mdd_percent == 0:
        return None
    return cagr_percent / abs(mdd_percent)


def historical_var_pct(returns: pd.Series, confidence: float = 0.95) -> float | None:
    """Historical one-day Value at Risk, reported as a positive loss magnitude."""
    if len(returns) < MIN_OBS_FOR_VOLATILITY:
        return None
    quantile = float(np.quantile(returns.to_numpy(), 1.0 - confidence))
    return abs(quantile) * 100.0


def historical_cvar_pct(returns: pd.Series, confidence: float = 0.95) -> float | None:
    """Expected shortfall: mean loss on days worse than the VaR threshold."""
    if len(returns) < MIN_OBS_FOR_VOLATILITY:
        return None
    values = returns.to_numpy()
    threshold = float(np.quantile(values, 1.0 - confidence))
    tail = values[values <= threshold]
    if tail.size == 0:
        return None
    return abs(float(tail.mean())) * 100.0


def positive_day_ratio(returns: pd.Series) -> float | None:
    """Share of trading days with a non-negative return, in percent."""
    if returns.empty:
        return None
    return float((returns >= 0).mean() * 100.0)


# ---------------------------------------------------------------------------
# Benchmark-relative metrics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkComparison:
    """Fund performance measured against a benchmark series."""

    benchmark_code: str
    benchmark_name: str
    overlap_days: int
    beta: float | None = None
    alpha_pct: float | None = None
    r_squared: float | None = None
    tracking_error_pct: float | None = None
    information_ratio: float | None = None
    correlation: float | None = None
    up_capture_pct: float | None = None
    down_capture_pct: float | None = None
    excess_return_1y_pct: float | None = None


def align(fund_nav: pd.Series, bench_nav: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Restrict both NAV series to their common dates, preserving order."""
    common = fund_nav.index.intersection(bench_nav.index)
    return fund_nav.loc[common], bench_nav.loc[common]


def capture_ratios(
    fund_monthly: pd.Series, bench_monthly: pd.Series
) -> tuple[float | None, float | None]:
    """Up/down capture: geometric fund return over geometric benchmark return,
    computed separately across months when the benchmark rose and fell.

    100% down-capture means the fund fell exactly as much as the benchmark;
    below 100% is better. Monthly (not daily) frequency is used because that is
    the industry-standard convention for capture statistics.
    """
    common = fund_monthly.index.intersection(bench_monthly.index)
    fund, bench = fund_monthly.loc[common], bench_monthly.loc[common]

    def geometric(series: pd.Series) -> float:
        return float(np.prod(1.0 + series.to_numpy()) - 1.0)

    up_mask, down_mask = bench > 0, bench < 0
    up = down = None
    if up_mask.sum() >= 3:
        bench_up = geometric(bench[up_mask])
        if bench_up != 0:
            up = geometric(fund[up_mask]) / bench_up * 100.0
    if down_mask.sum() >= 3:
        bench_down = geometric(bench[down_mask])
        if bench_down != 0:
            down = geometric(fund[down_mask]) / bench_down * 100.0
    return up, down


def compare_to_benchmark(
    fund_nav: pd.Series,
    bench_nav: pd.Series,
    annual_risk_free: float,
    benchmark_code: str,
    benchmark_name: str,
) -> BenchmarkComparison:
    """Full benchmark-relative block: beta, Jensen's alpha, R^2, TE, IR, capture.

    Beta and alpha come from an OLS regression of fund excess returns on
    benchmark excess returns (the CAPM market model), estimated on the overlapping
    trading days only.
    """
    fund, bench = align(fund_nav, bench_nav)
    fund_ret, bench_ret = daily_returns(fund), daily_returns(bench)
    common = fund_ret.index.intersection(bench_ret.index)
    fund_ret, bench_ret = fund_ret.loc[common], bench_ret.loc[common]
    result = {"overlap_days": len(common)}

    if len(common) >= MIN_OBS_FOR_REGRESSION:
        rf_daily = daily_risk_free(annual_risk_free)
        fx, bx = fund_ret - rf_daily, bench_ret - rf_daily
        bench_var = float(bx.var(ddof=1))
        if bench_var > 0:
            beta = float(np.cov(fx, bx, ddof=1)[0, 1] / bench_var)
            alpha_daily = float(fx.mean() - beta * bx.mean())
            corr = float(np.corrcoef(fund_ret, bench_ret)[0, 1])
            active = fund_ret - bench_ret
            te = float(active.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR) * 100.0)
            result.update(
                beta=beta,
                # Alpha is annualised geometrically so it composes with CAGR figures.
                alpha_pct=((1.0 + alpha_daily) ** TRADING_DAYS_PER_YEAR - 1.0) * 100.0,
                r_squared=corr**2,
                correlation=corr,
                tracking_error_pct=te,
                information_ratio=(
                    float(active.mean() * TRADING_DAYS_PER_YEAR * 100.0) / te if te else None
                ),
            )

    up, down = capture_ratios(monthly_returns(fund), monthly_returns(bench))
    result.update(up_capture_pct=up, down_capture_pct=down)

    fund_1y, bench_1y = trailing_return_pct(fund, 1.0), trailing_return_pct(bench, 1.0)
    if fund_1y is not None and bench_1y is not None:
        result["excess_return_1y_pct"] = fund_1y - bench_1y

    return BenchmarkComparison(
        benchmark_code=benchmark_code, benchmark_name=benchmark_name, **result
    )


# ---------------------------------------------------------------------------
# Rolling-return consistency
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RollingReturnStats:
    """Distribution of overlapping annualised returns over a fixed window."""

    window_years: float
    observations: int
    min_pct: float
    median_pct: float
    max_pct: float
    positive_share_pct: float
    beat_hurdle_share_pct: float | None = None
    hurdle_pct: float | None = None


def rolling_return_stats(
    nav: pd.Series, window_years: float = 3.0, hurdle_pct: float | None = None
) -> RollingReturnStats | None:
    """Distribution of every overlapping ``window_years`` annualised return.

    Point-to-point returns are hostage to their start date; the rolling
    distribution is what tells you whether a fund was *consistently* good.
    Windows are matched by calendar date (nearest prior trading day), so weekends
    and holidays never shift the window.
    """
    if len(nav) < MIN_OBS_FOR_VOLATILITY:
        return None
    window_days = round(window_years * DAYS_PER_YEAR)
    start_targets = nav.index - pd.Timedelta(days=window_days)
    # searchsorted gives, for each end date, the insertion point of its window
    # start; ``- 1`` steps back to the nearest trading day at or before it.
    positions = nav.index.searchsorted(start_targets, side="right") - 1
    values = nav.to_numpy()
    valid = positions >= 0
    if not valid.any():
        return None
    end_values, start_values = values[valid], values[positions[valid]]
    # Drop windows where the "start" is the end itself (no elapsed time).
    elapsed = (nav.index[valid] - nav.index[positions[valid]]).days.to_numpy()
    keep = elapsed >= window_days * 0.9
    if keep.sum() < 5:
        return None
    end_values, start_values, elapsed = end_values[keep], start_values[keep], elapsed[keep]
    annualised = ((end_values / start_values) ** (DAYS_PER_YEAR / elapsed) - 1.0) * 100.0

    beat = None
    if hurdle_pct is not None:
        beat = float((annualised > hurdle_pct).mean() * 100.0)
    return RollingReturnStats(
        window_years=window_years,
        observations=int(annualised.size),
        min_pct=float(np.min(annualised)),
        median_pct=float(np.median(annualised)),
        max_pct=float(np.max(annualised)),
        positive_share_pct=float((annualised > 0).mean() * 100.0),
        beat_hurdle_share_pct=beat,
        hurdle_pct=hurdle_pct,
    )


# ---------------------------------------------------------------------------
# Cashflow-based returns (SIP / XIRR)
# ---------------------------------------------------------------------------


def xirr(cashflows: Sequence[tuple[date, float]], guess: float = 0.1) -> float | None:
    """Internal rate of return for irregularly timed cashflows (Excel's XIRR).

    Solves ``sum(cf_i / (1 + r) ** (days_i / 365)) = 0`` with Newton-Raphson,
    falling back to bisection when the derivative is badly behaved. Uses a 365-day
    year to match Excel/most Indian fund factsheets.

    Returns ``None`` when the cashflows have no sign change (no IRR exists) or the
    solver fails to bracket a root.
    """
    if len(cashflows) < 2:
        return None
    flows = sorted(cashflows, key=lambda item: item[0])
    amounts = np.array([amount for _, amount in flows], dtype=float)
    if not (amounts.max() > 0 and amounts.min() < 0):
        return None
    t0 = flows[0][0]
    years = np.array([(d - t0).days / 365.0 for d, _ in flows], dtype=float)

    def npv(rate: float) -> float:
        return float(np.sum(amounts / (1.0 + rate) ** years))

    rate = guess
    for _ in range(100):
        value = npv(rate)
        if abs(value) < 1e-9:
            return rate
        derivative = float(np.sum(-years * amounts / (1.0 + rate) ** (years + 1.0)))
        if derivative == 0 or not np.isfinite(derivative):
            break
        step = value / derivative
        candidate = rate - step
        if candidate <= -0.9999 or not np.isfinite(candidate):
            break
        if abs(step) < 1e-10:
            return candidate
        rate = candidate

    low, high = -0.9999, 10.0
    f_low, f_high = npv(low), npv(high)
    if f_low * f_high > 0:
        return None
    for _ in range(200):
        mid = (low + high) / 2.0
        f_mid = npv(mid)
        if abs(f_mid) < 1e-10:
            return mid
        if f_low * f_mid <= 0:
            high, f_high = mid, f_mid
        else:
            low, f_low = mid, f_mid
    return (low + high) / 2.0


def sip_xirr_pct(
    nav: pd.Series, monthly_amount: float = 10_000.0, years: float = 3.0, day_of_month: int = 1
) -> float | None:
    """XIRR of a monthly SIP over the trailing ``years``, in percent.

    Models the realistic investor experience: a fixed rupee amount invested on
    the first available trading day at or after ``day_of_month`` each month,
    valued at the latest NAV. This is the number that differs most from headline
    CAGR, because it weights recent contributions heavily.
    """
    if len(nav) < MIN_OBS_FOR_VOLATILITY:
        return None
    end_date = nav.index[-1]
    start_date = end_date - pd.DateOffset(days=round(years * DAYS_PER_YEAR))
    if nav.index[0] > start_date:
        return None

    installment_dates = pd.date_range(
        start=start_date.replace(day=day_of_month), end=end_date, freq="MS"
    )
    units = 0.0
    cashflows: list[tuple[date, float]] = []
    for target in installment_dates:
        # First trading day at or after the nominal SIP date.
        upcoming = nav.loc[target:]
        if upcoming.empty:
            continue
        trade_date, trade_nav = upcoming.index[0], float(upcoming.iloc[0])
        if trade_date >= end_date:
            continue
        units += monthly_amount / trade_nav
        cashflows.append((trade_date.date(), -monthly_amount))

    if len(cashflows) < 2:
        return None
    cashflows.append((end_date.date(), units * float(nav.iloc[-1])))
    rate = xirr(cashflows)
    return rate * 100.0 if rate is not None else None


# ---------------------------------------------------------------------------
# Full metric bundle
# ---------------------------------------------------------------------------


@dataclass
class SchemeMetrics:
    """Complete metric bundle for one scheme. ``None`` always means "not enough data"."""

    scheme_code: str
    scheme_name: str
    fund_house: str | None
    scheme_category: str | None
    data_source: str
    as_of: date
    first_date: date
    observations: int
    history_years: float
    latest_nav: float

    sma_50: float | None = None
    sma_200: float | None = None
    sma_signal: str = "INSUFFICIENT_HISTORY"
    nav_vs_sma50_pct: float | None = None

    return_3m_pct: float | None = None
    return_6m_pct: float | None = None
    return_1y_pct: float | None = None
    cagr_3y_pct: float | None = None
    cagr_5y_pct: float | None = None
    cagr_since_inception_pct: float | None = None
    sip_xirr_3y_pct: float | None = None

    volatility_pct: float | None = None
    downside_deviation_pct: float | None = None
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    calmar_ratio: float | None = None
    max_drawdown_pct: float | None = None
    max_drawdown_peak: date | None = None
    max_drawdown_trough: date | None = None
    max_drawdown_recovered: bool | None = None
    max_drawdown_days: int | None = None
    current_drawdown_pct: float | None = None
    var_95_pct: float | None = None
    cvar_95_pct: float | None = None
    positive_days_pct: float | None = None

    rolling_3y: RollingReturnStats | None = None
    benchmark: BenchmarkComparison | None = None
    notes: list[str] = field(default_factory=list)

    def as_row(self) -> dict[str, object]:
        """Flat dict for CSV/DataFrame export, rounded for human consumption."""

        def r(value: float | None, digits: int = 2) -> float | None:
            return round(value, digits) if isinstance(value, (int, float)) else None

        row: dict[str, object] = {
            "scheme_code": self.scheme_code,
            "scheme_name": self.scheme_name,
            "fund_house": self.fund_house,
            "scheme_category": self.scheme_category,
            "as_of": self.as_of.isoformat(),
            "data_source": self.data_source,
            "observations": self.observations,
            "history_years": r(self.history_years, 1),
            "latest_nav": r(self.latest_nav),
            "sma_50": r(self.sma_50),
            "sma_200": r(self.sma_200),
            "sma_signal": self.sma_signal,
            "return_3m_pct": r(self.return_3m_pct),
            "return_6m_pct": r(self.return_6m_pct),
            "return_1y_pct": r(self.return_1y_pct),
            "cagr_3y_pct": r(self.cagr_3y_pct),
            "cagr_5y_pct": r(self.cagr_5y_pct),
            "cagr_since_inception_pct": r(self.cagr_since_inception_pct),
            "sip_xirr_3y_pct": r(self.sip_xirr_3y_pct),
            "volatility_pct": r(self.volatility_pct),
            "downside_deviation_pct": r(self.downside_deviation_pct),
            "sharpe_ratio": r(self.sharpe_ratio),
            "sortino_ratio": r(self.sortino_ratio),
            "calmar_ratio": r(self.calmar_ratio),
            "max_drawdown_pct": r(self.max_drawdown_pct),
            "max_drawdown_trough": (
                self.max_drawdown_trough.isoformat() if self.max_drawdown_trough else None
            ),
            "max_drawdown_recovered": self.max_drawdown_recovered,
            "current_drawdown_pct": r(self.current_drawdown_pct),
            "var_95_pct": r(self.var_95_pct),
            "cvar_95_pct": r(self.cvar_95_pct),
            "positive_days_pct": r(self.positive_days_pct),
        }
        if self.rolling_3y:
            row.update(
                rolling_3y_min_pct=r(self.rolling_3y.min_pct),
                rolling_3y_median_pct=r(self.rolling_3y.median_pct),
                rolling_3y_max_pct=r(self.rolling_3y.max_pct),
                rolling_3y_positive_share_pct=r(self.rolling_3y.positive_share_pct),
            )
        if self.benchmark:
            row.update(
                benchmark_code=self.benchmark.benchmark_code,
                beta=r(self.benchmark.beta),
                alpha_pct=r(self.benchmark.alpha_pct),
                r_squared=r(self.benchmark.r_squared, 3),
                tracking_error_pct=r(self.benchmark.tracking_error_pct),
                information_ratio=r(self.benchmark.information_ratio),
                up_capture_pct=r(self.benchmark.up_capture_pct, 1),
                down_capture_pct=r(self.benchmark.down_capture_pct, 1),
                excess_return_1y_pct=r(self.benchmark.excess_return_1y_pct),
            )
        return row


def sma_signal(latest_nav: float, sma_value: float | None, observations: int, window: int) -> str:
    """Trend label that refuses to fire until the full SMA window is populated.

    A 50-day average computed from 12 observations is not a 50-day average, and
    labelling it BULLISH would be a fabricated signal.
    """
    if sma_value is None or observations < window:
        return "INSUFFICIENT_HISTORY"
    return "BULLISH" if latest_nav >= sma_value else "BEARISH"


def compute_scheme_metrics(
    nav: pd.Series,
    *,
    scheme_code: str,
    scheme_name: str,
    fund_house: str | None = None,
    scheme_category: str | None = None,
    data_source: str = "unknown",
    annual_risk_free: float = 0.065,
    benchmark_nav: pd.Series | None = None,
    benchmark_code: str | None = None,
    benchmark_name: str | None = None,
    sip_amount: float = 10_000.0,
) -> SchemeMetrics:
    """Compute the full metric bundle for one scheme's NAV series.

    Raises ``ValueError`` on an empty series -- an empty scheme is a data problem
    for `validation.py` to report, not a row of ``None`` to quietly publish.
    """
    if nav.empty:
        raise ValueError(f"scheme {scheme_code}: empty NAV series")

    returns = daily_returns(nav)
    latest_nav = float(nav.iloc[-1])
    observations = len(nav)
    history_years = (nav.index[-1] - nav.index[0]).days / DAYS_PER_YEAR

    sma50 = float(nav.rolling(50).mean().iloc[-1]) if observations >= 50 else None
    sma200 = float(nav.rolling(200).mean().iloc[-1]) if observations >= 200 else None
    cagr3 = trailing_return_pct(nav, 3.0)
    mdd = max_drawdown(nav)
    current_dd = float((latest_nav / float(nav.cummax().iloc[-1]) - 1.0) * 100.0)

    metrics = SchemeMetrics(
        scheme_code=scheme_code,
        scheme_name=scheme_name,
        fund_house=fund_house,
        scheme_category=scheme_category,
        data_source=data_source,
        as_of=nav.index[-1].date(),
        first_date=nav.index[0].date(),
        observations=observations,
        history_years=history_years,
        latest_nav=latest_nav,
        sma_50=sma50,
        sma_200=sma200,
        sma_signal=sma_signal(latest_nav, sma50, observations, 50),
        nav_vs_sma50_pct=((latest_nav / sma50 - 1.0) * 100.0) if sma50 else None,
        return_3m_pct=trailing_return_pct(nav, 0.25),
        return_6m_pct=trailing_return_pct(nav, 0.5),
        return_1y_pct=trailing_return_pct(nav, 1.0),
        cagr_3y_pct=cagr3,
        cagr_5y_pct=trailing_return_pct(nav, 5.0),
        cagr_since_inception_pct=since_inception_cagr_pct(nav),
        sip_xirr_3y_pct=sip_xirr_pct(nav, monthly_amount=sip_amount, years=3.0),
        volatility_pct=annualised_volatility_pct(returns),
        downside_deviation_pct=downside_deviation_pct(
            returns, mar_daily=daily_risk_free(annual_risk_free)
        ),
        sharpe_ratio=sharpe_ratio(returns, annual_risk_free),
        sortino_ratio=sortino_ratio(returns, annual_risk_free),
        max_drawdown_pct=mdd.depth_pct if mdd else None,
        max_drawdown_peak=mdd.peak_date if mdd else None,
        max_drawdown_trough=mdd.trough_date if mdd else None,
        max_drawdown_recovered=mdd.recovered if mdd else None,
        max_drawdown_days=mdd.drawdown_days if mdd else None,
        current_drawdown_pct=current_dd,
        var_95_pct=historical_var_pct(returns),
        cvar_95_pct=historical_cvar_pct(returns),
        positive_days_pct=positive_day_ratio(returns),
        rolling_3y=rolling_return_stats(nav, 3.0, hurdle_pct=annual_risk_free * 100.0),
    )
    metrics.calmar_ratio = calmar_ratio(cagr3, metrics.max_drawdown_pct)

    if benchmark_nav is not None and not benchmark_nav.empty and benchmark_code != scheme_code:
        metrics.benchmark = compare_to_benchmark(
            nav,
            benchmark_nav,
            annual_risk_free,
            benchmark_code or "benchmark",
            benchmark_name or "Benchmark",
        )

    if history_years < 3:
        metrics.notes.append(
            f"Only {history_years:.1f}y of history: 3Y/5Y and rolling statistics are "
            "unavailable or thin."
        )
    if metrics.data_source == "synthetic":
        metrics.notes.append("SYNTHETIC DATA -- not investable analysis.")
    return metrics


def growth_of(nav: pd.Series, initial: float = 10_000.0) -> pd.Series:
    """Rebase a NAV series to a common starting investment for comparison charts."""
    if nav.empty:
        return nav
    return nav / float(nav.iloc[0]) * initial


def drawdown_series(nav: pd.Series) -> pd.Series:
    """Percentage drawdown from the running peak, at every point in time."""
    return (nav / nav.cummax() - 1.0) * 100.0


def summarise(values: Iterable[float | None]) -> float | None:
    """Mean of the non-``None`` values, or ``None`` when everything is missing."""
    clean = [v for v in values if v is not None and np.isfinite(v)]
    return float(np.mean(clean)) if clean else None
