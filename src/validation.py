"""
Data-quality gate that runs *before* any metric is computed.

A financial report is only as good as its inputs, so this module answers one
question per scheme: is this NAV history fit to publish numbers from? Findings
carry a severity, and CRITICAL findings can fail the pipeline (`--fail-on-critical`)
rather than shipping a plausible-looking but wrong report.

Checks implemented:

| Check                  | Severity        | Why it matters                              |
|------------------------|-----------------|---------------------------------------------|
| No data for scheme     | CRITICAL        | Nothing to analyse                          |
| Non-positive NAV       | CRITICAL        | Breaks every ratio and log-return           |
| Duplicate (code, date) | CRITICAL        | Double-counts a day in return series        |
| Future-dated NAV       | CRITICAL        | Clock/parse bug; corrupts "latest"          |
| Too few observations   | CRITICAL        | Metrics would be noise                      |
| Stale feed             | WARNING         | Report headline would be silently outdated  |
| Extreme daily move     | WARNING         | Usually a bad NAV, occasionally a real event|
| Calendar gap           | WARNING         | Understates volatility, distorts drawdown   |
| Missing metadata       | WARNING         | Report shows an unnamed scheme              |
| Synthetic data         | WARNING         | Numbers are fabricated, not investable      |
| Short history          | INFO            | Long-horizon metrics unavailable            |
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

import pandas as pd

from src import config

logger = logging.getLogger(__name__)

Severity = Literal["CRITICAL", "WARNING", "INFO"]
_ORDER: dict[str, int] = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}


@dataclass(frozen=True)
class Finding:
    """A single data-quality observation about one scheme."""

    scheme_code: str
    severity: Severity
    check: str
    message: str
    affected_rows: int = 0

    def __str__(self) -> str:
        return f"[{self.severity}] {self.scheme_code} {self.check}: {self.message}"


@dataclass
class QualityReport:
    """All findings across all schemes, plus per-scheme coverage facts."""

    findings: list[Finding] = field(default_factory=list)
    coverage: dict[str, dict[str, object]] = field(default_factory=dict)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    @property
    def critical(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "CRITICAL"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "WARNING"]

    @property
    def has_critical(self) -> bool:
        return bool(self.critical)

    def unusable_schemes(self) -> set[str]:
        """Schemes with a CRITICAL finding -- excluded from the analysis."""
        return {f.scheme_code for f in self.critical}

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: (_ORDER[f.severity], f.scheme_code, f.check))

    def summary(self) -> dict[str, int]:
        return {
            "critical": len(self.critical),
            "warnings": len(self.warnings),
            "info": sum(1 for f in self.findings if f.severity == "INFO"),
            "schemes_checked": len(self.coverage),
            "schemes_excluded": len(self.unusable_schemes()),
        }


def validate_nav_frame(
    frame: pd.DataFrame,
    *,
    today: date | None = None,
    max_staleness_days: int = config.MAX_STALENESS_DAYS,
    extreme_move_pct: float = config.EXTREME_DAILY_MOVE_PCT,
    max_gap_business_days: int = config.MAX_GAP_BUSINESS_DAYS,
    min_observations: int = config.MIN_OBSERVATIONS,
    expected_schemes: list[str] | None = None,
) -> QualityReport:
    """Validate a long-format NAV frame (one row per scheme-date).

    Expected columns: ``scheme_code, date, nav`` plus optional ``scheme_name``
    and ``data_source``.
    """
    report = QualityReport()
    today = today or datetime.now().date()

    if frame.empty:
        for code in expected_schemes or ["*"]:
            report.add(Finding(code, "CRITICAL", "no_data", "No NAV rows found in the database"))
        return report

    frame = frame.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    frame["scheme_code"] = frame["scheme_code"].astype(str)

    present = set(frame["scheme_code"].unique())
    for code in set(expected_schemes or []) - present:
        report.add(Finding(code, "CRITICAL", "no_data", "Requested scheme has no NAV rows stored"))

    for code, group in frame.groupby("scheme_code", sort=True):
        report.coverage[code] = _check_scheme(
            report,
            str(code),
            group,
            today=today,
            max_staleness_days=max_staleness_days,
            extreme_move_pct=extreme_move_pct,
            max_gap_business_days=max_gap_business_days,
            min_observations=min_observations,
        )

    logger.info(
        "Data quality: %s critical, %s warnings across %s schemes",
        len(report.critical),
        len(report.warnings),
        len(report.coverage),
    )
    return report


def _check_scheme(
    report: QualityReport,
    code: str,
    group: pd.DataFrame,
    *,
    today: date,
    max_staleness_days: int,
    extreme_move_pct: float,
    max_gap_business_days: int,
    min_observations: int,
) -> dict[str, object]:
    group = group.sort_values("date")
    name = str(group["scheme_name"].iloc[-1]) if "scheme_name" in group else ""
    source = str(group["data_source"].iloc[-1]) if "data_source" in group else "unknown"

    # --- integrity -------------------------------------------------------
    bad_nav = group["nav"].isna() | (group["nav"] <= 0)
    if bad_nav.any():
        report.add(
            Finding(
                code,
                "CRITICAL",
                "non_positive_nav",
                f"{int(bad_nav.sum())} rows with null or non-positive NAV",
                int(bad_nav.sum()),
            )
        )

    duplicates = int(group["date"].duplicated().sum())
    if duplicates:
        report.add(
            Finding(
                code, "CRITICAL", "duplicate_dates", f"{duplicates} duplicate dates", duplicates
            )
        )

    future = int((group["date"].dt.date > today).sum())
    if future:
        report.add(
            Finding(
                code, "CRITICAL", "future_dated", f"{future} NAV rows dated in the future", future
            )
        )

    observations = len(group)
    if observations < min_observations:
        report.add(
            Finding(
                code,
                "CRITICAL",
                "insufficient_history",
                f"Only {observations} observations (minimum {min_observations})",
                observations,
            )
        )

    # --- freshness -------------------------------------------------------
    last_date = group["date"].iloc[-1].date()
    staleness = (today - last_date).days
    if staleness > max_staleness_days:
        report.add(
            Finding(
                code,
                "WARNING",
                "stale_data",
                f"Latest NAV is {staleness} days old ({last_date.isoformat()})",
            )
        )

    # --- continuity and outliers ----------------------------------------
    if observations >= 2:
        moves = group["nav"].pct_change().abs() * 100
        extreme = moves > extreme_move_pct
        if extreme.any():
            worst_date = group.loc[moves.idxmax(), "date"].date()
            report.add(
                Finding(
                    code,
                    "WARNING",
                    "extreme_move",
                    f"{int(extreme.sum())} day(s) moved more than {extreme_move_pct:.0f}% "
                    f"(worst {moves.max():.1f}% on {worst_date}); verify against AMFI",
                    int(extreme.sum()),
                )
            )

        # Count business days between consecutive observations; a legitimate
        # weekend is 1 business-day step, so anything above the threshold is a hole.
        dates = group["date"].to_numpy(dtype="datetime64[D]")
        gaps = pd.Series(
            [len(pd.bdate_range(dates[i], dates[i + 1])) - 2 for i in range(len(dates) - 1)]
        )
        big_gaps = gaps[gaps > max_gap_business_days]
        if not big_gaps.empty:
            report.add(
                Finding(
                    code,
                    "WARNING",
                    "calendar_gap",
                    f"{len(big_gaps)} gap(s) over {max_gap_business_days} business days "
                    f"(largest {int(big_gaps.max())} days); volatility may be understated",
                    len(big_gaps),
                )
            )

    # --- metadata and provenance ----------------------------------------
    if not name or name.startswith("Unknown scheme"):
        report.add(
            Finding(code, "WARNING", "missing_metadata", "No scheme_info row; name unavailable")
        )
    if source == "synthetic":
        report.add(
            Finding(
                code,
                "WARNING",
                "synthetic_data",
                "NAVs are synthetic (offline fallback) -- not investable analysis",
            )
        )

    history_years = (group["date"].iloc[-1] - group["date"].iloc[0]).days / 365.25
    if history_years < 3 and observations >= min_observations:
        report.add(
            Finding(
                code,
                "INFO",
                "short_history",
                f"{history_years:.1f} years of history; 3Y/5Y metrics limited",
            )
        )

    return {
        "scheme_name": name,
        "observations": observations,
        "first_date": group["date"].iloc[0].date().isoformat(),
        "last_date": last_date.isoformat(),
        "staleness_days": staleness,
        "history_years": round(history_years, 2),
        "data_source": source,
    }
