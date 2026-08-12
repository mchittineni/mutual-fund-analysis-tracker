"""
Central configuration for the Indian Mutual Fund tracker.

Every value can be overridden by an environment variable so the same code runs
unchanged locally, in CI, and in a container. Directories are *not* created at
import time -- side effects on import make the module untestable and surprise
anyone who merely wants to read a constant. Call `ensure_directories()` instead.
"""

from __future__ import annotations

import os
from pathlib import Path

# --- Paths -----------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("MF_DATA_DIR", BASE_DIR / "data"))
RAW_DATA_DIR = DATA_DIR / "raw"
PROCESSED_DATA_DIR = DATA_DIR / "processed"
REPORT_DIR = Path(os.getenv("MF_REPORT_DIR", DATA_DIR / "reports"))
DB_PATH = Path(os.getenv("MF_DB_PATH", DATA_DIR / "mf_database.db"))
PERFORMANCE_REPORT_FILE = DATA_DIR / "latest_fund_performance.csv"

# --- Universe --------------------------------------------------------------

# AMFI scheme codes analysed when none are supplied on the command line.
DEFAULT_TARGET_SCHEMES: list[str] = [
    "119598",  # SBI Bluechip Fund - Direct Plan - Growth
    "125497",  # HDFC Top 100 Fund - Direct Plan - Growth
    "120503",  # Parag Parikh Flexi Cap Fund - Direct Plan - Growth
]

# Benchmark scheme. An index *fund* is used as the benchmark proxy because AMFI
# publishes NAVs, not index levels; its NAV tracks the index minus a small TER,
# so alpha computed against it is already net-of-index-cost. Set to None (or the
# MF_BENCHMARK_SCHEME env var) to skip all benchmark-relative metrics.
BENCHMARK_SCHEME: str | None = os.getenv("MF_BENCHMARK_SCHEME", "120716")
BENCHMARK_LABEL = os.getenv("MF_BENCHMARK_LABEL", "UTI Nifty 50 Index Fund (index proxy)")

# --- Financial assumptions -------------------------------------------------

# Annual risk-free rate as a decimal. Default approximates the 10-year Indian
# government security yield. Every Sharpe/Sortino/alpha figure moves with this,
# so it is surfaced in the report's assumptions block.
RISK_FREE_RATE = float(os.getenv("MF_RISK_FREE_RATE", "0.065"))
SIP_MONTHLY_AMOUNT = float(os.getenv("MF_SIP_AMOUNT", "10000"))
GROWTH_CHART_INITIAL = 10_000.0

# --- Data quality thresholds ----------------------------------------------

# Latest NAV older than this many calendar days means the feed is stale. AMFI
# publishes every business day; 7 days tolerates a long weekend plus holidays.
MAX_STALENESS_DAYS = int(os.getenv("MF_MAX_STALENESS_DAYS", "7"))
# A single-day move beyond this is almost always a bad NAV, not a market event.
EXTREME_DAILY_MOVE_PCT = float(os.getenv("MF_EXTREME_MOVE_PCT", "20"))
# Consecutive missing business days that count as a hole in the history.
MAX_GAP_BUSINESS_DAYS = int(os.getenv("MF_MAX_GAP_DAYS", "5"))
# Below this many observations no metric is trustworthy.
MIN_OBSERVATIONS = int(os.getenv("MF_MIN_OBSERVATIONS", "30"))

# --- Fetching --------------------------------------------------------------

FETCH_MAX_RETRIES = int(os.getenv("MF_FETCH_RETRIES", "3"))
FETCH_BACKOFF_SECONDS = float(os.getenv("MF_FETCH_BACKOFF", "2.0"))
FETCH_POLITE_DELAY_SECONDS = float(os.getenv("MF_FETCH_DELAY", "1.0"))

# --- Reporting -------------------------------------------------------------

REPORT_TITLE = "Indian Mutual Fund Performance & Risk Report"
DISCLAIMER = (
    "This report is generated from publicly available AMFI NAV data for research and "
    "educational purposes only. It is not investment advice, and past performance does "
    "not predict future returns. Mutual fund investments are subject to market risk."
)


def ensure_directories() -> None:
    """Create the data/report directory tree. Safe to call repeatedly."""
    for directory in (DATA_DIR, RAW_DATA_DIR, PROCESSED_DATA_DIR, REPORT_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def assumptions() -> dict[str, object]:
    """The assumption set every report must disclose alongside its numbers."""
    return {
        "risk_free_rate_annual_pct": round(RISK_FREE_RATE * 100, 2),
        "trading_days_per_year": 252,
        "days_per_year_for_cagr": 365.25,
        "return_convention": "Simple daily returns; sub-1y horizons absolute, >=1y annualised",
        "benchmark": BENCHMARK_LABEL if BENCHMARK_SCHEME else "none",
        "sip_monthly_amount_inr": SIP_MONTHLY_AMOUNT,
        "staleness_tolerance_days": MAX_STALENESS_DAYS,
        "nav_type": "Direct plan, growth option (as published by AMFI)",
        "tax_and_exit_load": "Ignored -- all returns are pre-tax and gross of exit load",
    }
