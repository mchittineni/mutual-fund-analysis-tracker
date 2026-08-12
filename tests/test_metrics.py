"""
Known-answer tests for the quantitative core.

Each test pins a metric against a closed-form or hand-computable answer rather
than a previously observed output, so a refactor that changes a formula fails
loudly instead of silently re-baselining.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from src import metrics

# --- series preparation ----------------------------------------------------


def test_to_nav_series_sorts_dedupes_and_drops_bad_rows():
    frame = pd.DataFrame(
        {
            "date": ["2024-01-03", "2024-01-01", "2024-01-02", "2024-01-02", "2024-01-04"],
            "nav": [103.0, 101.0, 102.0, 102.5, -5.0],
        }
    )
    series = metrics.to_nav_series(frame)
    assert list(series.index.strftime("%Y-%m-%d")) == ["2024-01-01", "2024-01-02", "2024-01-03"]
    # Duplicate 2024-01-02 keeps the LAST value (a restated NAV supersedes).
    assert series.iloc[1] == 102.5
    # The non-positive NAV is dropped, not clamped.
    assert (series > 0).all()


def test_daily_risk_free_compounds_to_the_annual_rate():
    daily = metrics.daily_risk_free(0.065)
    assert math.isclose((1 + daily) ** 252 - 1, 0.065, rel_tol=1e-9)


def test_nav_asof_uses_the_nearest_prior_trading_day():
    index = pd.to_datetime(["2024-01-05", "2024-01-08", "2024-01-09"])  # Fri, Mon, Tue
    nav = pd.Series([100.0, 101.0, 102.0], index=index)
    # A Saturday resolves back to Friday, never forward to Monday.
    assert metrics.nav_asof(nav, pd.Timestamp("2024-01-06")) == 100.0
    assert metrics.nav_asof(nav, pd.Timestamp("2024-01-09")) == 102.0
    assert metrics.nav_asof(nav, pd.Timestamp("2023-12-31")) is None


# --- return metrics --------------------------------------------------------


def test_absolute_return_matches_hand_calculation():
    assert metrics.absolute_return_pct(100.0, 125.0) == pytest.approx(25.0)
    assert metrics.absolute_return_pct(0.0, 125.0) is None
    assert metrics.absolute_return_pct(None, 125.0) is None


def test_cagr_doubling_over_three_years_is_the_cube_root_of_two():
    expected = (2 ** (1 / 3) - 1) * 100
    assert metrics.cagr_pct(100.0, 200.0, 3.0) == pytest.approx(expected)


def test_cagr_refuses_sub_monthly_horizons():
    """Annualising a few days produces explosive nonsense, so it must return None."""
    assert metrics.cagr_pct(100.0, 101.0, 0.02) is None


def test_trailing_return_is_absolute_below_one_year_and_annualised_above(compounding_nav):
    six_month = metrics.trailing_return_pct(compounding_nav, 0.5)
    one_year = metrics.trailing_return_pct(compounding_nav, 1.0)
    # Series compounds at 10%/year: 6-month absolute is ~sqrt(1.1)-1 = 4.88%.
    assert six_month == pytest.approx(4.88, abs=0.15)
    assert one_year == pytest.approx(10.0, abs=0.1)


def test_trailing_return_returns_none_when_history_is_too_short(compounding_nav):
    short = compounding_nav.iloc[-100:]
    assert metrics.trailing_return_pct(short, 3.0) is None


def test_since_inception_cagr_recovers_the_construction_rate(compounding_nav):
    assert metrics.since_inception_cagr_pct(compounding_nav) == pytest.approx(10.0, abs=0.05)


# --- risk metrics ----------------------------------------------------------


def test_flat_series_has_zero_volatility_and_no_drawdown(flat_nav):
    returns = metrics.daily_returns(flat_nav)
    assert metrics.annualised_volatility_pct(returns) == pytest.approx(0.0)
    drawdown = metrics.max_drawdown(flat_nav)
    assert drawdown.depth_pct == pytest.approx(0.0)
    assert drawdown.recovered is True


def test_annualised_volatility_scales_daily_sigma_by_root_252():
    rng = np.random.default_rng(7)
    daily = pd.Series(rng.normal(0.0, 0.01, 500))
    expected = daily.std(ddof=1) * math.sqrt(252) * 100
    assert metrics.annualised_volatility_pct(daily) == pytest.approx(expected)


def test_volatility_is_none_below_the_minimum_observation_count():
    assert metrics.annualised_volatility_pct(pd.Series([0.01, -0.01])) is None


def test_sharpe_is_zero_when_return_equals_the_risk_free_rate():
    """A series earning exactly the risk-free rate every day has zero excess return."""
    rf_daily = metrics.daily_risk_free(0.065)
    returns = pd.Series([rf_daily] * 300)
    # Zero variance in excess returns -> undefined ratio, reported as None.
    assert metrics.sharpe_ratio(returns, 0.065) is None


def test_sharpe_matches_the_textbook_formula():
    rng = np.random.default_rng(11)
    returns = pd.Series(rng.normal(0.0006, 0.008, 800))
    excess = returns - metrics.daily_risk_free(0.05)
    expected = excess.mean() / excess.std(ddof=1) * math.sqrt(252)
    assert metrics.sharpe_ratio(returns, 0.05) == pytest.approx(expected)


def test_sortino_ignores_upside_volatility():
    """Two series with identical downside but different upside: Sortino ranks them apart,
    and the one with bigger upside must score higher."""
    base = [-0.01] * 50 + [0.01] * 250
    spiky = [-0.01] * 50 + [0.03] * 250
    calm_ratio = metrics.sortino_ratio(pd.Series(base), 0.0)
    spiky_ratio = metrics.sortino_ratio(pd.Series(spiky), 0.0)
    assert spiky_ratio > calm_ratio > 0


def test_downside_deviation_is_zero_when_nothing_falls_below_the_mar():
    returns = pd.Series([0.01] * 100)
    assert metrics.downside_deviation_pct(returns, mar_daily=0.0) == pytest.approx(0.0)


def test_max_drawdown_identifies_depth_peak_trough_and_recovery(drawdown_nav):
    result = metrics.max_drawdown(drawdown_nav)
    assert result.depth_pct == pytest.approx(-25.0, abs=0.5)
    assert result.peak_date < result.trough_date
    assert result.recovered is True
    assert result.recovery_date is not None and result.recovery_date > result.trough_date
    assert result.drawdown_days > 0


def test_max_drawdown_reports_unrecovered_when_the_series_ends_underwater():
    index = pd.bdate_range("2024-01-01", periods=60)
    values = list(np.linspace(100, 150, 30)) + list(np.linspace(150, 120, 30))
    result = metrics.max_drawdown(pd.Series(values, index=index))
    assert result.recovered is False
    assert result.recovery_date is None
    assert result.depth_pct == pytest.approx(-20.0, abs=0.5)


def test_calmar_is_cagr_over_absolute_drawdown():
    assert metrics.calmar_ratio(15.0, -30.0) == pytest.approx(0.5)
    assert metrics.calmar_ratio(15.0, 0.0) is None
    assert metrics.calmar_ratio(None, -30.0) is None


def test_var_and_cvar_are_positive_magnitudes_and_ordered():
    rng = np.random.default_rng(3)
    returns = pd.Series(rng.normal(0.0, 0.012, 2000))
    var = metrics.historical_var_pct(returns, 0.95)
    cvar = metrics.historical_cvar_pct(returns, 0.95)
    assert var > 0 and cvar > 0
    # Expected shortfall is always at least as severe as VaR.
    assert cvar >= var


def test_positive_day_ratio():
    returns = pd.Series([0.01, -0.01, 0.0, 0.02])
    assert metrics.positive_day_ratio(returns) == pytest.approx(75.0)


# --- benchmark-relative ----------------------------------------------------


def test_beta_of_a_series_against_itself_is_one_with_zero_alpha(compounding_nav):
    rng = np.random.default_rng(5)
    noisy = compounding_nav * (
        1 + pd.Series(rng.normal(0, 0.004, len(compounding_nav)), index=compounding_nav.index)
    )
    comparison = metrics.compare_to_benchmark(noisy, noisy, 0.06, "X", "X")
    assert comparison.beta == pytest.approx(1.0, abs=1e-9)
    assert comparison.alpha_pct == pytest.approx(0.0, abs=1e-6)
    assert comparison.r_squared == pytest.approx(1.0, abs=1e-9)
    assert comparison.tracking_error_pct == pytest.approx(0.0, abs=1e-9)


def test_double_leverage_produces_beta_of_two():
    """A fund whose daily returns are exactly 2x the benchmark's must show beta ~2."""
    rng = np.random.default_rng(13)
    index = pd.bdate_range("2019-01-01", periods=900)
    bench_returns = rng.normal(0.0004, 0.01, len(index) - 1)
    bench = pd.Series(100 * np.cumprod(np.r_[1, 1 + bench_returns]), index=index)
    fund = pd.Series(100 * np.cumprod(np.r_[1, 1 + 2 * bench_returns]), index=index)
    comparison = metrics.compare_to_benchmark(fund, bench, 0.0, "B", "Bench")
    assert comparison.beta == pytest.approx(2.0, abs=0.02)
    assert comparison.r_squared == pytest.approx(1.0, abs=1e-6)


def test_benchmark_metrics_are_none_without_enough_overlap():
    index = pd.bdate_range("2024-01-01", periods=30)
    series = pd.Series(np.linspace(100, 110, 30), index=index)
    comparison = metrics.compare_to_benchmark(series, series, 0.06, "B", "Bench")
    assert comparison.overlap_days == 29
    assert comparison.beta is None  # below MIN_OBS_FOR_REGRESSION


def test_capture_ratios_are_100_percent_against_itself():
    rng = np.random.default_rng(17)
    index = pd.bdate_range("2020-01-01", periods=800)
    series = pd.Series(100 * np.cumprod(1 + rng.normal(0.0004, 0.01, 800)), index=index)
    up, down = metrics.capture_ratios(
        metrics.monthly_returns(series), metrics.monthly_returns(series)
    )
    assert up == pytest.approx(100.0)
    assert down == pytest.approx(100.0)


def test_align_restricts_to_common_dates():
    a = pd.Series([1.0, 2, 3], index=pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]))
    b = pd.Series([9.0, 8], index=pd.to_datetime(["2024-01-02", "2024-01-03"]))
    left, right = metrics.align(a, b)
    assert len(left) == len(right) == 2
    assert list(left) == [2.0, 3.0]


# --- rolling returns -------------------------------------------------------


def test_rolling_returns_of_a_constant_grower_collapse_to_that_rate(compounding_nav):
    stats = metrics.rolling_return_stats(compounding_nav, window_years=2.0)
    assert stats is not None
    assert stats.min_pct == pytest.approx(10.0, abs=0.2)
    assert stats.max_pct == pytest.approx(10.0, abs=0.2)
    assert stats.positive_share_pct == pytest.approx(100.0)


def test_rolling_returns_none_when_window_exceeds_history(flat_nav):
    assert metrics.rolling_return_stats(flat_nav, window_years=10.0) is None


def test_rolling_returns_track_the_hurdle_share(compounding_nav):
    stats = metrics.rolling_return_stats(compounding_nav, window_years=2.0, hurdle_pct=6.5)
    assert stats.beat_hurdle_share_pct == pytest.approx(100.0)
    stats_high = metrics.rolling_return_stats(compounding_nav, window_years=2.0, hurdle_pct=20.0)
    assert stats_high.beat_hurdle_share_pct == pytest.approx(0.0)


# --- XIRR ------------------------------------------------------------------


def test_xirr_of_a_single_year_doubling_is_100_percent():
    flows = [(date(2023, 1, 1), -1000.0), (date(2024, 1, 1), 2000.0)]
    rate = metrics.xirr(flows)
    # 365 days at 100% -> exactly 1.0 (Excel's day-count convention).
    assert rate == pytest.approx(1.0, abs=1e-4)


def test_xirr_zero_return_is_zero_rate():
    flows = [(date(2023, 1, 1), -1000.0), (date(2024, 1, 1), 1000.0)]
    assert metrics.xirr(flows) == pytest.approx(0.0, abs=1e-6)


def test_xirr_returns_none_without_a_sign_change():
    assert metrics.xirr([(date(2023, 1, 1), -100.0), (date(2024, 1, 1), -100.0)]) is None
    assert metrics.xirr([(date(2023, 1, 1), 100.0)]) is None


def test_xirr_handles_irregular_multi_flow_schedules():
    flows = [
        (date(2022, 1, 1), -1000.0),
        (date(2022, 7, 15), -500.0),
        (date(2023, 3, 2), -750.0),
        (date(2024, 1, 1), 2500.0),
    ]
    rate = metrics.xirr(flows)
    assert rate is not None
    # Verify the root: NPV at the solved rate must be ~0.
    npv = sum(amount / (1 + rate) ** ((day - flows[0][0]).days / 365) for day, amount in flows)
    assert npv == pytest.approx(0.0, abs=1e-4)


def test_sip_xirr_on_a_steady_grower_approximates_its_growth_rate(compounding_nav):
    """A SIP into a smoothly 10%-compounding fund should earn close to 10%."""
    result = metrics.sip_xirr_pct(compounding_nav, monthly_amount=10_000, years=3.0)
    assert result == pytest.approx(10.0, abs=1.0)


def test_sip_xirr_is_none_without_enough_history(flat_nav):
    assert metrics.sip_xirr_pct(flat_nav.iloc[-10:], years=3.0) is None


# --- SMA signal and bundle -------------------------------------------------


def test_sma_signal_refuses_to_fire_before_the_window_is_full():
    """A 50-day average built from 12 observations is not a 50-day average."""
    assert metrics.sma_signal(100.0, 95.0, observations=12, window=50) == "INSUFFICIENT_HISTORY"
    assert metrics.sma_signal(100.0, 95.0, observations=200, window=50) == "BULLISH"
    assert metrics.sma_signal(90.0, 95.0, observations=200, window=50) == "BEARISH"
    assert metrics.sma_signal(90.0, None, observations=200, window=50) == "INSUFFICIENT_HISTORY"


def test_compute_scheme_metrics_populates_the_full_bundle(compounding_nav):
    result = metrics.compute_scheme_metrics(
        compounding_nav,
        scheme_code="111111",
        scheme_name="Steady Growth Fund",
        data_source="amfi",
        annual_risk_free=0.065,
    )
    assert result.scheme_code == "111111"
    assert result.observations == len(compounding_nav)
    assert result.cagr_3y_pct == pytest.approx(10.0, abs=0.1)
    assert result.sma_50 is not None and result.sma_200 is not None
    assert result.sma_signal == "BULLISH"  # a monotone riser is always above its average
    assert result.max_drawdown_pct == pytest.approx(0.0, abs=1e-9)
    assert result.rolling_3y is not None
    assert result.benchmark is None  # none supplied
    assert result.as_row()["scheme_code"] == "111111"


def test_compute_scheme_metrics_flags_synthetic_data(compounding_nav):
    result = metrics.compute_scheme_metrics(
        compounding_nav, scheme_code="1", scheme_name="X", data_source="synthetic"
    )
    assert any("SYNTHETIC" in note for note in result.notes)


def test_compute_scheme_metrics_rejects_an_empty_series():
    with pytest.raises(ValueError, match="empty NAV series"):
        metrics.compute_scheme_metrics(pd.Series(dtype=float), scheme_code="1", scheme_name="X")


def test_short_history_produces_none_not_a_fabricated_number():
    index = pd.bdate_range(end="2024-06-28", periods=25)
    nav = pd.Series(np.linspace(100, 104, 25), index=index)
    result = metrics.compute_scheme_metrics(nav, scheme_code="1", scheme_name="Young Fund")
    assert result.cagr_3y_pct is None
    assert result.sma_50 is None
    assert result.sma_signal == "INSUFFICIENT_HISTORY"
    assert result.volatility_pct is not None  # 24 returns clears the 20-observation floor
    assert any("history" in note for note in result.notes)


def test_growth_of_rebases_to_the_initial_amount(compounding_nav):
    rebased = metrics.growth_of(compounding_nav, 10_000)
    assert rebased.iloc[0] == pytest.approx(10_000)
    assert rebased.iloc[-1] > 10_000


def test_drawdown_series_is_never_positive(drawdown_nav):
    series = metrics.drawdown_series(drawdown_nav)
    assert series.max() <= 1e-9
    assert series.min() == pytest.approx(-25.0, abs=0.5)


def test_summarise_ignores_missing_values():
    assert metrics.summarise([1.0, None, 3.0]) == pytest.approx(2.0)
    assert metrics.summarise([None, None]) is None


def test_monthly_returns_compound_correctly():
    index = pd.bdate_range("2024-01-01", "2024-03-31")
    nav = pd.Series(np.linspace(100, 110, len(index)), index=index)
    monthly = metrics.monthly_returns(nav)
    assert len(monthly) == 2  # Feb and Mar have a prior month-end to compare against
    assert (monthly > 0).all()
