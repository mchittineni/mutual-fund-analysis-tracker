"""
Analysis orchestration: database -> quality gate -> metrics -> ranked insights.

This module owns the *interpretation* layer. `metrics.py` produces numbers;
`analyzer.py` decides which numbers matter, ranks schemes, and derives the
findings a reader should act on. Anything judgemental (thresholds for "high
volatility", what counts as a drawdown alert) lives here and is named, so the
editorial choices are reviewable rather than buried in a report template.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from src import config, db_manager, metrics, validation
from src.metrics import SchemeMetrics

logger = logging.getLogger(__name__)

# Interpretation thresholds -- editorial judgement, deliberately explicit.
HIGH_VOLATILITY_PCT = 20.0
STRONG_SHARPE = 1.0
WEAK_SHARPE = 0.3
DEEP_DRAWDOWN_PCT = -15.0
CONCENTRATION_R2 = 0.95


@dataclass
class Insight:
    """One interpreted finding, ranked so the report can lead with what matters."""

    rank: int
    category: str
    headline: str
    detail: str
    scheme_codes: list[str] = field(default_factory=list)


@dataclass
class AnalysisResult:
    """Everything a report needs: metrics, quality findings, insights, assumptions."""

    generated_at: datetime
    as_of: date | None
    schemes: list[SchemeMetrics]
    quality: validation.QualityReport
    insights: list[Insight]
    assumptions: dict[str, object]
    benchmark_code: str | None = None
    benchmark_name: str | None = None
    nav_series: dict[str, pd.Series] = field(default_factory=dict)

    @property
    def has_synthetic_data(self) -> bool:
        return any(s.data_source == "synthetic" for s in self.schemes)

    def to_frame(self) -> pd.DataFrame:
        """Flat metric table, one row per scheme."""
        if not self.schemes:
            return pd.DataFrame()
        return pd.DataFrame([s.as_row() for s in self.schemes])

    def ranked_by(self, attribute: str, descending: bool = True) -> list[SchemeMetrics]:
        """Schemes ordered by a metric, excluding those where it is unavailable."""
        available = [s for s in self.schemes if getattr(s, attribute, None) is not None]
        return sorted(available, key=lambda s: getattr(s, attribute), reverse=descending)


# ---------------------------------------------------------------------------
# Technical indicators on the full panel (kept for the dashboard and CSV)
# ---------------------------------------------------------------------------


def add_technical_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    """Add 50D/200D SMAs per scheme.

    ``min_periods`` equals the window: a 50-day average built from 12 points is
    not a 50-day average, so early rows are left NaN instead of being filled with
    a shorter, misleading mean.
    """
    frame = frame.sort_values(["scheme_code", "date"]).copy()
    grouped = frame.groupby("scheme_code")["nav"]
    frame["50D_SMA"] = grouped.transform(lambda x: x.rolling(50, min_periods=50).mean())
    frame["200D_SMA"] = grouped.transform(lambda x: x.rolling(200, min_periods=200).mean())
    return frame


# ---------------------------------------------------------------------------
# Insight generation
# ---------------------------------------------------------------------------


def derive_insights(
    schemes: list[SchemeMetrics], quality: validation.QualityReport
) -> list[Insight]:
    """Turn the metric table into a ranked list of things a reader should notice.

    Ordering is deliberate: data-integrity problems outrank performance
    commentary, because a reader must know the numbers are trustworthy before
    they act on them.
    """
    insights: list[Insight] = []

    def add(category: str, headline: str, detail: str, codes: list[str] | None = None) -> None:
        insights.append(Insight(len(insights) + 1, category, headline, detail, codes or []))

    # 1. Integrity first.
    if any(s.data_source == "synthetic" for s in schemes):
        synthetic = [s.scheme_code for s in schemes if s.data_source == "synthetic"]
        add(
            "data-integrity",
            "Report contains SYNTHETIC data -- not investable",
            f"Scheme(s) {', '.join(synthetic)} use generated NAVs because the live AMFI "
            "fetch was unavailable. Treat every figure for them as a pipeline test, not analysis.",
            synthetic,
        )
    if quality.has_critical:
        excluded = sorted(quality.unusable_schemes())
        add(
            "data-integrity",
            f"{len(excluded)} scheme(s) excluded for data-quality failures",
            "Excluded: "
            + ", ".join(excluded)
            + ". See the data-quality section for the checks that failed.",
            excluded,
        )
    stale = [f.scheme_code for f in quality.warnings if f.check == "stale_data"]
    if stale:
        add(
            "data-integrity",
            "Stale NAV feed detected",
            f"Scheme(s) {', '.join(sorted(set(stale)))} have not published a fresh NAV within "
            f"{config.MAX_STALENESS_DAYS} days; headline figures are older than they appear.",
            sorted(set(stale)),
        )

    if not schemes:
        return insights

    # 2. Risk-adjusted leadership -- the number that actually ranks funds.
    by_sharpe = [s for s in schemes if s.sharpe_ratio is not None]
    if by_sharpe:
        best = max(by_sharpe, key=lambda s: s.sharpe_ratio)
        worst = min(by_sharpe, key=lambda s: s.sharpe_ratio)
        add(
            "performance",
            f"Best risk-adjusted return: {best.scheme_name}",
            f"Sharpe {best.sharpe_ratio:.2f} and Sortino {_fmt(best.sortino_ratio)} on "
            f"{_fmt(best.volatility_pct)}% annualised volatility. "
            f"Sharpe is excess return over a {config.RISK_FREE_RATE * 100:.2f}% risk-free rate "
            "per unit of volatility -- it is the ranking metric, not raw return.",
            [best.scheme_code],
        )
        if len(by_sharpe) > 1 and worst.sharpe_ratio < WEAK_SHARPE:
            add(
                "performance",
                f"Weakest risk-adjusted return: {worst.scheme_name}",
                f"Sharpe {worst.sharpe_ratio:.2f} (below {WEAK_SHARPE}) means the return earned "
                "barely compensated for the volatility taken.",
                [worst.scheme_code],
            )

    # 3. Raw return leadership -- and whether it survives a risk adjustment.
    by_cagr = [s for s in schemes if s.cagr_3y_pct is not None]
    if by_cagr:
        top = max(by_cagr, key=lambda s: s.cagr_3y_pct)
        detail = f"3Y CAGR {top.cagr_3y_pct:.2f}% (SIP XIRR {_fmt(top.sip_xirr_3y_pct)}%)."
        if (
            by_sharpe
            and top.scheme_code != max(by_sharpe, key=lambda s: s.sharpe_ratio).scheme_code
        ):
            detail += (
                " Note it is *not* the risk-adjusted leader -- the extra return came with "
                "extra volatility."
            )
        add("performance", f"Highest 3Y CAGR: {top.scheme_name}", detail, [top.scheme_code])

    # 4. Drawdown / downside exposure.
    deep = [
        s
        for s in schemes
        if s.max_drawdown_pct is not None and s.max_drawdown_pct <= DEEP_DRAWDOWN_PCT
    ]
    if deep:
        worst = min(deep, key=lambda s: s.max_drawdown_pct)
        unrecovered = [s.scheme_code for s in deep if s.max_drawdown_recovered is False]
        detail = (
            f"{worst.scheme_name} fell {worst.max_drawdown_pct:.1f}% from its "
            f"{worst.max_drawdown_peak} peak to {worst.max_drawdown_trough}"
        )
        detail += (
            f" and has not recovered ({worst.max_drawdown_days} days underwater)."
            if worst.max_drawdown_recovered is False
            else f" and recovered after {worst.max_drawdown_days} days."
        )
        if unrecovered:
            detail += f" Still underwater: {', '.join(unrecovered)}."
        add("risk", f"Deepest drawdown: {worst.max_drawdown_pct:.1f}%", detail, [worst.scheme_code])

    in_drawdown = [
        s for s in schemes if s.current_drawdown_pct is not None and s.current_drawdown_pct < -5
    ]
    if in_drawdown:
        add(
            "risk",
            f"{len(in_drawdown)} scheme(s) currently below their all-time-high NAV",
            "; ".join(f"{s.scheme_name} {s.current_drawdown_pct:.1f}%" for s in in_drawdown),
            [s.scheme_code for s in in_drawdown],
        )

    volatile = [s for s in schemes if (s.volatility_pct or 0) > HIGH_VOLATILITY_PCT]
    if volatile:
        add(
            "risk",
            f"{len(volatile)} scheme(s) above {HIGH_VOLATILITY_PCT:.0f}% annualised volatility",
            "; ".join(f"{s.scheme_name} {s.volatility_pct:.1f}%" for s in volatile)
            + f". 95% one-day VaR reaches {max(s.var_95_pct or 0 for s in volatile):.2f}%.",
            [s.scheme_code for s in volatile],
        )

    # 5. Benchmark-relative behaviour.
    benched = [s for s in schemes if s.benchmark and s.benchmark.alpha_pct is not None]
    if benched:
        best_alpha = max(benched, key=lambda s: s.benchmark.alpha_pct)
        bench_name = best_alpha.benchmark.benchmark_name
        add(
            "benchmark",
            f"Highest alpha vs {bench_name}: {best_alpha.scheme_name}",
            f"Annualised alpha {best_alpha.benchmark.alpha_pct:+.2f}% with beta "
            f"{_fmt(best_alpha.benchmark.beta)}, tracking error "
            f"{_fmt(best_alpha.benchmark.tracking_error_pct)}%, information ratio "
            f"{_fmt(best_alpha.benchmark.information_ratio)}.",
            [best_alpha.scheme_code],
        )
        laggards = [s for s in benched if (s.benchmark.excess_return_1y_pct or 0) < 0]
        if laggards:
            add(
                "benchmark",
                f"{len(laggards)} scheme(s) trailed the benchmark over 1 year",
                "; ".join(
                    f"{s.scheme_name} {s.benchmark.excess_return_1y_pct:+.2f}%" for s in laggards
                ),
                [s.scheme_code for s in laggards],
            )
        closet = [s for s in benched if (s.benchmark.r_squared or 0) > CONCENTRATION_R2]
        if closet:
            add(
                "benchmark",
                f"{len(closet)} scheme(s) look index-like (R-squared > {CONCENTRATION_R2})",
                "; ".join(f"{s.scheme_name} R2 {s.benchmark.r_squared:.3f}" for s in closet)
                + ". Active fees buy little differentiation at this level of index tracking.",
                [s.scheme_code for s in closet],
            )
        weak_downside = [s for s in benched if (s.benchmark.down_capture_pct or 0) > 105]
        if weak_downside:
            add(
                "benchmark",
                "Poor downside protection",
                "; ".join(
                    f"{s.scheme_name} captures {s.benchmark.down_capture_pct:.0f}% of benchmark "
                    "declines"
                    for s in weak_downside
                ),
                [s.scheme_code for s in weak_downside],
            )

    # 6. Consistency, which point-to-point returns hide entirely.
    rolling = [s for s in schemes if s.rolling_3y]
    if rolling:
        least = min(rolling, key=lambda s: s.rolling_3y.min_pct)
        add(
            "consistency",
            "Rolling 3Y returns show the real spread",
            "; ".join(
                f"{s.scheme_name}: {s.rolling_3y.min_pct:.1f}% to {s.rolling_3y.max_pct:.1f}% "
                f"(median {s.rolling_3y.median_pct:.1f}%, positive in "
                f"{s.rolling_3y.positive_share_pct:.0f}% of windows)"
                for s in rolling
            )
            + f". Worst observed 3Y window belongs to {least.scheme_name}.",
            [s.scheme_code for s in rolling],
        )

    # 7. Trend state.
    bearish = [s for s in schemes if s.sma_signal == "BEARISH"]
    if bearish:
        add(
            "trend",
            f"{len(bearish)} scheme(s) trading below their 50-day average",
            "; ".join(f"{s.scheme_name} ({s.nav_vs_sma50_pct:+.1f}% vs 50D SMA)" for s in bearish)
            + ". A moving-average cross is a momentum signal only; it is not a valuation signal.",
            [s.scheme_code for s in bearish],
        )
    return insights


def _fmt(value: float | None, digits: int = 2) -> str:
    return f"{value:.{digits}f}" if isinstance(value, (int, float)) else "n/a"


# ---------------------------------------------------------------------------
# Top-level analysis
# ---------------------------------------------------------------------------


def analyse(
    db_path: str | Path | None = None,
    scheme_codes: list[str] | None = None,
    *,
    benchmark_code: str | None = config.BENCHMARK_SCHEME,
    risk_free_rate: float = config.RISK_FREE_RATE,
    sip_amount: float = config.SIP_MONTHLY_AMOUNT,
    today: date | None = None,
) -> AnalysisResult:
    """Load, validate, and analyse the requested schemes.

    Schemes with CRITICAL quality findings are excluded from the metric table but
    remain in the quality report -- silence about a broken scheme would be worse
    than a visible exclusion. The benchmark is loaded even when not in
    ``scheme_codes`` so relative metrics are always available.
    """
    codes_to_load = list(scheme_codes) if scheme_codes else None
    if codes_to_load and benchmark_code and benchmark_code not in codes_to_load:
        codes_to_load = [*codes_to_load, benchmark_code]

    frame = db_manager.load_data(db_path, codes_to_load)
    quality = validation.validate_nav_frame(frame, today=today, expected_schemes=scheme_codes)

    if frame.empty:
        logger.warning("No NAV data available; returning an empty analysis")
        return AnalysisResult(
            generated_at=datetime.now(),
            as_of=None,
            schemes=[],
            quality=quality,
            insights=derive_insights([], quality),
            assumptions=config.assumptions(),
            benchmark_code=benchmark_code,
        )

    frame["scheme_code"] = frame["scheme_code"].astype(str)
    series_by_code: dict[str, pd.Series] = {
        str(code): metrics.to_nav_series(group)
        for code, group in frame.groupby("scheme_code", sort=True)
    }
    meta_by_code = {
        str(code): group.iloc[-1] for code, group in frame.groupby("scheme_code", sort=True)
    }

    benchmark_nav = series_by_code.get(str(benchmark_code)) if benchmark_code else None
    benchmark_name = config.BENCHMARK_LABEL
    if benchmark_code and str(benchmark_code) in meta_by_code:
        benchmark_name = str(meta_by_code[str(benchmark_code)]["scheme_name"])

    unusable = quality.unusable_schemes()
    analysed: list[SchemeMetrics] = []
    # Report on requested schemes only; the benchmark is context, not a holding.
    requested = [str(c) for c in (scheme_codes or series_by_code.keys())]

    for code in requested:
        if code in unusable:
            logger.warning("Scheme %s excluded: critical data-quality findings", code)
            continue
        nav = series_by_code.get(code)
        if nav is None or nav.empty:
            continue
        meta = meta_by_code[code]
        analysed.append(
            metrics.compute_scheme_metrics(
                nav,
                scheme_code=code,
                scheme_name=str(meta["scheme_name"]),
                fund_house=_optional(meta.get("fund_house")),
                scheme_category=_optional(meta.get("scheme_category")),
                data_source=str(meta.get("data_source") or "unknown"),
                annual_risk_free=risk_free_rate,
                benchmark_nav=benchmark_nav,
                benchmark_code=str(benchmark_code) if benchmark_code else None,
                benchmark_name=benchmark_name,
                sip_amount=sip_amount,
            )
        )

    assumptions = config.assumptions()
    assumptions["risk_free_rate_annual_pct"] = round(risk_free_rate * 100, 2)
    assumptions["sip_monthly_amount_inr"] = sip_amount
    assumptions["benchmark"] = benchmark_name if benchmark_nav is not None else "none available"

    result = AnalysisResult(
        generated_at=datetime.now(),
        as_of=max((s.as_of for s in analysed), default=None),
        schemes=analysed,
        quality=quality,
        insights=[],
        assumptions=assumptions,
        benchmark_code=str(benchmark_code) if benchmark_code else None,
        benchmark_name=benchmark_name if benchmark_nav is not None else None,
        nav_series={code: series_by_code[code] for code in series_by_code},
    )
    result.insights = derive_insights(analysed, quality)
    logger.info(
        "Analysed %s scheme(s); %s insight(s); %s",
        len(analysed),
        len(result.insights),
        quality.summary(),
    )
    return result


def _optional(value: object) -> str | None:
    """Normalise pandas NaN/None metadata into a real ``None``."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return str(value)


def generate_weekly_report(db_path: str | Path | None = None) -> pd.DataFrame:
    """Backwards-compatible entry point: analyse the default universe and write the CSV.

    Retained because the original pipeline, notebook, and README all call it.
    """
    result = analyse(db_path, scheme_codes=config.DEFAULT_TARGET_SCHEMES)
    frame = result.to_frame()
    if frame.empty:
        logger.warning("Nothing to report -- run the fetch step first")
        return frame
    config.ensure_directories()
    frame.to_csv(config.PERFORMANCE_REPORT_FILE, index=False)
    logger.info("Metric table written to %s", config.PERFORMANCE_REPORT_FILE)
    return frame


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(generate_weekly_report().to_string(index=False))
