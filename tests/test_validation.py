"""Tests for the data-quality gate: each check must fire on its own trigger and stay quiet otherwise."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from src import validation


def make_frame(
    *,
    code: str = "111111",
    name: str = "Test Fund",
    observations: int = 400,
    end: date | None = None,
    source: str = "amfi",
) -> pd.DataFrame:
    end = end or date.today()
    index = pd.bdate_range(end=pd.Timestamp(end), periods=observations)
    return pd.DataFrame(
        {
            "scheme_code": code,
            "scheme_name": name,
            "date": index,
            "nav": np.linspace(100, 150, observations),
            "data_source": source,
        }
    )


def checks(report: validation.QualityReport) -> set[str]:
    return {finding.check for finding in report.findings}


def test_clean_data_produces_no_critical_findings():
    report = validation.validate_nav_frame(make_frame())
    assert not report.has_critical
    assert "stale_data" not in checks(report)
    assert report.coverage["111111"]["observations"] == 400


def test_empty_frame_is_critical_for_every_expected_scheme():
    report = validation.validate_nav_frame(pd.DataFrame(), expected_schemes=["1", "2"])
    assert report.has_critical
    assert report.unusable_schemes() == {"1", "2"}


def test_requested_scheme_absent_from_the_data_is_critical():
    report = validation.validate_nav_frame(
        make_frame(code="111111"), expected_schemes=["111111", "999999"]
    )
    assert "999999" in report.unusable_schemes()
    assert "111111" not in report.unusable_schemes()


def test_non_positive_nav_is_critical():
    frame = make_frame()
    frame.loc[5, "nav"] = 0.0
    frame.loc[6, "nav"] = -3.0
    report = validation.validate_nav_frame(frame)
    assert "non_positive_nav" in checks(report)
    assert report.has_critical


def test_duplicate_dates_are_critical():
    frame = make_frame()
    frame = pd.concat([frame, frame.iloc[[10]]], ignore_index=True)
    report = validation.validate_nav_frame(frame)
    assert "duplicate_dates" in checks(report)


def test_future_dated_rows_are_critical():
    frame = make_frame()
    frame.loc[len(frame) - 1, "date"] = pd.Timestamp(date.today() + timedelta(days=10))
    report = validation.validate_nav_frame(frame)
    assert "future_dated" in checks(report)


def test_too_few_observations_is_critical():
    report = validation.validate_nav_frame(make_frame(observations=10))
    assert "insufficient_history" in checks(report)
    assert report.has_critical


def test_stale_feed_is_a_warning_not_a_failure():
    """Stale data is still analysable -- the reader just needs to know it is old."""
    report = validation.validate_nav_frame(make_frame(end=date.today() - timedelta(days=30)))
    assert "stale_data" in checks(report)
    assert not report.has_critical
    assert report.coverage["111111"]["staleness_days"] >= 30


def test_extreme_daily_move_is_flagged_for_manual_verification():
    frame = make_frame()
    frame.loc[100, "nav"] = frame.loc[99, "nav"] * 1.5  # +50% in one day
    report = validation.validate_nav_frame(frame)
    assert "extreme_move" in checks(report)
    finding = next(f for f in report.findings if f.check == "extreme_move")
    assert finding.severity == "WARNING"
    assert "verify against AMFI" in finding.message


def test_calendar_gap_is_flagged():
    frame = make_frame(observations=300)
    # Remove a month of observations from the middle of the history.
    frame = pd.concat([frame.iloc[:100], frame.iloc[130:]], ignore_index=True)
    report = validation.validate_nav_frame(frame)
    assert "calendar_gap" in checks(report)


def test_weekends_and_holidays_do_not_count_as_gaps():
    """Business-day-aware gap detection must not fire on ordinary weekends."""
    report = validation.validate_nav_frame(make_frame(observations=500))
    assert "calendar_gap" not in checks(report)


def test_missing_metadata_is_a_warning():
    frame = make_frame(name="Unknown scheme 111111")
    report = validation.validate_nav_frame(frame)
    assert "missing_metadata" in checks(report)


def test_synthetic_data_is_always_flagged():
    report = validation.validate_nav_frame(make_frame(source="synthetic"))
    finding = next(f for f in report.findings if f.check == "synthetic_data")
    assert finding.severity == "WARNING"
    assert "not investable" in finding.message


def test_short_history_is_informational_only():
    frame = make_frame(observations=200)  # <1y of business days
    report = validation.validate_nav_frame(frame)
    assert "short_history" in checks(report)
    assert not report.has_critical


def test_summary_counts_and_exclusions_are_consistent():
    good = make_frame(code="111111")
    bad = make_frame(code="222222", observations=5)
    report = validation.validate_nav_frame(pd.concat([good, bad], ignore_index=True))
    summary = report.summary()
    assert summary["schemes_checked"] == 2
    assert summary["schemes_excluded"] == 1
    assert report.unusable_schemes() == {"222222"}


def test_findings_sort_critical_first():
    frame = make_frame(observations=10, source="synthetic")
    report = validation.validate_nav_frame(frame)
    severities = [f.severity for f in report.sorted_findings()]
    assert severities[0] == "CRITICAL"
    assert severities == sorted(
        severities, key=lambda s: {"CRITICAL": 0, "WARNING": 1, "INFO": 2}[s]
    )


def test_thresholds_are_configurable_per_call():
    frame = make_frame(end=date.today() - timedelta(days=10))
    lenient = validation.validate_nav_frame(frame, max_staleness_days=30)
    strict = validation.validate_nav_frame(frame, max_staleness_days=2)
    assert "stale_data" not in checks(lenient)
    assert "stale_data" in checks(strict)


def test_finding_string_form_is_readable():
    finding = validation.Finding("111111", "WARNING", "stale_data", "3 days old")
    assert str(finding) == "[WARNING] 111111 stale_data: 3 days old"
