"""Shared fixtures. All tests are hermetic: no network, no shared database."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import pandas as pd
import pytest

from src import db_manager

# Environment variables the code reads. Cleared for every test so a developer's
# shell -- or a CI runner -- cannot change what the suite asserts.
_AMBIENT_ENV = (
    # Set by GitHub Actions. `--summary-file` defaults to it, so without this the
    # suite would append several full reports to the CI job summary.
    "GITHUB_STEP_SUMMARY",
    "MF_DATA_DIR",
    "MF_REPORT_DIR",
    "MF_DB_PATH",
    "MF_BENCHMARK_SCHEME",
    "MF_RISK_FREE_RATE",
    "MF_SIP_AMOUNT",
    "MF_MAX_STALENESS_DAYS",
    "MF_EXTREME_MOVE_PCT",
    "MF_MIN_OBSERVATIONS",
    "MF_LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    """Run every test against a clean environment."""
    for name in _AMBIENT_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def db_path(tmp_path):
    """An isolated SQLite file per test."""
    return tmp_path / "test.db"


@pytest.fixture
def conn(db_path):
    connection = db_manager.setup_database(db_path)
    yield connection
    connection.close()


def business_days(count: int, end: datetime | None = None) -> pd.DatetimeIndex:
    """`count` business days ending today (or at `end`)."""
    end = end or datetime.now()
    return pd.bdate_range(end=end, periods=count)


@pytest.fixture
def flat_nav() -> pd.Series:
    """A perfectly flat NAV series: zero return, zero volatility, zero drawdown."""
    index = business_days(300)
    return pd.Series(100.0, index=index, name="nav")


@pytest.fixture
def compounding_nav() -> pd.Series:
    """NAV compounding at exactly 10% per calendar year -- CAGR has a closed form."""
    index = business_days(1200)
    days = (index - index[0]).days.to_numpy()
    values = 100.0 * (1.10 ** (days / 365.25))
    return pd.Series(values, index=index, name="nav")


@pytest.fixture
def drawdown_nav() -> pd.Series:
    """Rises to 120, falls to 90 (a -25% drawdown), then fully recovers to 130."""
    index = business_days(120)
    segments = [
        *[100 + i * 0.5 for i in range(40)],  # 100 -> 119.5
        120.0,
        *[120 - (i + 1) * 0.75 for i in range(39)],  # 120 -> 90.75
        *[90.0 + i * 1.05 for i in range(40)],  # 90 -> 130.95
    ]
    return pd.Series(segments[: len(index)], index=index, name="nav").astype(float)


@pytest.fixture
def nav_frame(compounding_nav, drawdown_nav) -> pd.DataFrame:
    """Long-format frame for two schemes, in the shape `db_manager.load_data` returns."""
    frames = []
    for code, name, series in (
        ("111111", "Steady Growth Fund", compounding_nav),
        ("222222", "Volatile Fund", drawdown_nav),
    ):
        frames.append(
            pd.DataFrame(
                {
                    "scheme_code": code,
                    "scheme_name": name,
                    "fund_house": "Test AMC",
                    "scheme_category": "Equity",
                    "date": series.index,
                    "nav": series.to_numpy(),
                    "data_source": "amfi",
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


@pytest.fixture
def populated_db(db_path, nav_frame):
    """A database loaded with `nav_frame`, ready for analyzer/report tests."""
    conn = db_manager.setup_database(db_path)
    for code, group in nav_frame.groupby("scheme_code"):
        meta = group.iloc[0]
        db_manager.upsert_scheme_info(
            conn, str(code), meta["scheme_name"], "Test AMC", "Open Ended", "Equity", "amfi"
        )
        db_manager.upsert_nav_records(
            conn,
            [
                (str(code), d.strftime("%Y-%m-%d"), float(n))
                for d, n in zip(group["date"], group["nav"], strict=True)
            ],
            "amfi",
        )
    conn.close()
    return db_path


def approx(value: float, expected: float, tolerance: float = 1e-6) -> bool:
    return math.isclose(value, expected, rel_tol=tolerance, abs_tol=tolerance)
