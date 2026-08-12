"""
Tests for ingestion and the CLI.

Nothing here touches the network: `mftool` is replaced with fakes so retry,
partial-failure, and synthetic-gating behaviour is asserted deterministically.
"""

from __future__ import annotations

import pandas as pd
import pytest

import main_pipeline
from src import db_manager, fetch_amfi_data


class FakeMftool:
    """A stand-in for `mftool.Mftool` with programmable failures."""

    def __init__(self, fail_codes: set[str] | None = None, fail_times: int = 0):
        self.fail_codes = fail_codes or set()
        self.fail_times = fail_times
        self.attempts: dict[str, int] = {}

    def get_scheme_details(self, code: str) -> dict:
        return {
            "scheme_code": code,
            "scheme_name": f"Fund {code}",
            "fund_house": "Test AMC",
            "scheme_type": "Open Ended Schemes",
            "scheme_category": "Equity Scheme - Large Cap Fund",
        }

    def get_scheme_historical_nav(self, code: str) -> dict:
        self.attempts[code] = self.attempts.get(code, 0) + 1
        if code in self.fail_codes and self.attempts[code] <= (self.fail_times or 99):
            raise RuntimeError(f"simulated network failure for {code}")
        index = pd.bdate_range(end="2024-06-28", periods=400)
        return {
            "data": [
                {"date": day.strftime("%d-%m-%Y"), "nav": f"{100 + i * 0.1:.4f}"}
                for i, day in enumerate(index)
            ]
        }


@pytest.fixture
def fake_mftool(monkeypatch):
    def install(instance: FakeMftool) -> FakeMftool:
        monkeypatch.setattr(fetch_amfi_data, "MFTOOL_AVAILABLE", True)
        monkeypatch.setattr(fetch_amfi_data, "Mftool", lambda *_a, **_k: instance)
        return instance

    return install


# --- parsing ---------------------------------------------------------------


def test_parse_history_converts_dd_mm_yyyy_to_iso():
    payload = {"data": [{"date": "28-06-2024", "nav": "123.4567"}]}
    records = fetch_amfi_data._parse_history("111111", payload)
    assert records == [("111111", "2024-06-28", 123.4567)]


def test_parse_history_skips_unparseable_rows_without_failing_the_scheme():
    payload = {
        "data": [
            {"date": "28-06-2024", "nav": "100.0"},
            {"date": "29-06-2024", "nav": "N.A."},  # AMFI emits this on non-trading days
            {"date": "not-a-date", "nav": "101.0"},
            {"date": "30-06-2024", "nav": "0"},  # non-positive
        ]
    }
    records = fetch_amfi_data._parse_history("111111", payload)
    assert len(records) == 1


# --- retries ---------------------------------------------------------------


def test_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        return "ok"

    result = fetch_amfi_data._with_retries(flaky, "flaky", max_retries=5, sleep=lambda _: None)
    assert result == "ok"
    assert calls["n"] == 3


def test_retries_exhausted_raises_fetch_error():
    with pytest.raises(fetch_amfi_data.FetchError, match="after 3 attempts"):
        fetch_amfi_data._with_retries(
            lambda: (_ for _ in ()).throw(RuntimeError("always")),
            "always",
            max_retries=3,
            sleep=lambda _: None,
        )


# --- ingestion orchestration ----------------------------------------------


def test_successful_fetch_stores_metadata_and_navs(conn, fake_mftool):
    fake_mftool(FakeMftool())
    result = fetch_amfi_data.fetch_and_store_funds(["111111", "222222"], conn=conn, polite_delay=0)
    assert result.succeeded == ["111111", "222222"]
    assert result.rows_written == 800
    assert result.data_source == "amfi"
    assert not result.used_synthetic
    assert conn.execute("SELECT COUNT(*) FROM scheme_info").fetchone()[0] == 2


def test_one_bad_scheme_does_not_sink_the_run(conn, fake_mftool, monkeypatch):
    monkeypatch.setattr(fetch_amfi_data.config, "FETCH_MAX_RETRIES", 1)
    fake_mftool(FakeMftool(fail_codes={"222222"}))
    result = fetch_amfi_data.fetch_and_store_funds(["111111", "222222"], conn=conn, polite_delay=0)
    assert result.succeeded == ["111111"]
    assert "222222" in result.failed
    assert result.rows_written == 400


def test_total_failure_raises_rather_than_publishing_nothing(conn, fake_mftool, monkeypatch):
    monkeypatch.setattr(fetch_amfi_data.config, "FETCH_MAX_RETRIES", 1)
    fake_mftool(FakeMftool(fail_codes={"111111"}))
    with pytest.raises(fetch_amfi_data.FetchError, match="No scheme could be fetched"):
        fetch_amfi_data.fetch_and_store_funds(["111111"], conn=conn, polite_delay=0)


def test_missing_mftool_refuses_to_fabricate_data_by_default(conn, monkeypatch):
    """The critical safety property: no silent synthetic fallback."""
    monkeypatch.setattr(fetch_amfi_data, "MFTOOL_AVAILABLE", False)
    with pytest.raises(fetch_amfi_data.FetchError, match="refusing to fabricate"):
        fetch_amfi_data.fetch_and_store_funds(["111111"], conn=conn)
    assert conn.execute("SELECT COUNT(*) FROM nav_history").fetchone()[0] == 0


def test_synthetic_fallback_is_opt_in_and_labelled(conn, monkeypatch):
    monkeypatch.setattr(fetch_amfi_data, "MFTOOL_AVAILABLE", False)
    result = fetch_amfi_data.fetch_and_store_funds(["119598"], conn=conn, allow_synthetic=True)
    assert result.used_synthetic
    assert result.data_source == "synthetic"
    sources = {row[0] for row in conn.execute("SELECT DISTINCT data_source FROM nav_history")}
    assert sources == {"synthetic"}


def test_per_scheme_synthetic_fallback_marks_only_that_scheme(conn, fake_mftool, monkeypatch):
    monkeypatch.setattr(fetch_amfi_data.config, "FETCH_MAX_RETRIES", 1)
    fake_mftool(FakeMftool(fail_codes={"119598"}))
    result = fetch_amfi_data.fetch_and_store_funds(
        ["111111", "119598"], conn=conn, allow_synthetic=True, polite_delay=0
    )
    assert result.synthetic == ["119598"]
    assert result.data_source == "mixed"
    rows = dict(
        conn.execute("SELECT scheme_code, data_source FROM nav_history GROUP BY scheme_code")
    )
    assert rows["111111"] == "amfi"
    assert rows["119598"] == "synthetic"


def test_ingestion_is_audited_on_success_and_failure(conn, fake_mftool, monkeypatch):
    fake_mftool(FakeMftool())
    fetch_amfi_data.fetch_and_store_funds(["111111"], conn=conn, polite_delay=0)
    monkeypatch.setattr(fetch_amfi_data, "MFTOOL_AVAILABLE", False)
    with pytest.raises(fetch_amfi_data.FetchError):
        fetch_amfi_data.fetch_and_store_funds(["111111"], conn=conn)

    runs = list(conn.execute("SELECT status FROM ingestion_runs ORDER BY run_id"))
    assert [row["status"] for row in runs] == ["success", "failed"]


def test_synthetic_history_is_deterministic(conn):
    first = fetch_amfi_data.generate_synthetic_history(conn, ["119598"], years=2.0, seed=1)
    navs_a = [row[0] for row in conn.execute("SELECT nav FROM nav_history ORDER BY date")]
    conn.execute("DELETE FROM nav_history")
    fetch_amfi_data.generate_synthetic_history(conn, ["119598"], years=2.0, seed=1)
    navs_b = [row[0] for row in conn.execute("SELECT nav FROM nav_history ORDER BY date")]
    assert first > 0
    assert navs_a == navs_b


# --- CLI -------------------------------------------------------------------


def test_pipeline_end_to_end_writes_reports(tmp_path, fake_mftool, monkeypatch):
    fake_mftool(FakeMftool())
    monkeypatch.setattr(main_pipeline.config, "PERFORMANCE_REPORT_FILE", tmp_path / "legacy.csv")
    exit_code = main_pipeline.main(
        [
            "--schemes",
            "111111",
            "222222",
            "--benchmark",
            "none",
            "--db-path",
            str(tmp_path / "pipeline.db"),
            "--output-dir",
            str(tmp_path / "reports"),
        ]
    )
    assert exit_code == main_pipeline.EXIT_OK
    assert (tmp_path / "reports" / "index.html").exists()
    assert (tmp_path / "reports" / "report.md").exists()
    assert (tmp_path / "reports" / "report.json").exists()


def test_pipeline_returns_fetch_exit_code_when_ingestion_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_amfi_data, "MFTOOL_AVAILABLE", False)
    exit_code = main_pipeline.main(
        [
            "--schemes",
            "111111",
            "--db-path",
            str(tmp_path / "x.db"),
            "--output-dir",
            str(tmp_path / "r"),
        ]
    )
    assert exit_code == main_pipeline.EXIT_FETCH_FAILED


def test_pipeline_fails_on_critical_quality_when_asked(tmp_path):
    """`--fail-on-critical` is the CI gate: an empty database must not exit 0."""
    exit_code = main_pipeline.main(
        [
            "--skip-fetch",
            "--fail-on-critical",
            "--schemes",
            "111111",
            "--db-path",
            str(tmp_path / "empty.db"),
            "--output-dir",
            str(tmp_path / "r"),
        ]
    )
    assert exit_code == main_pipeline.EXIT_QUALITY_FAILED


def test_pipeline_writes_the_github_step_summary(tmp_path, fake_mftool, monkeypatch):
    """This is how the analysis surfaces on the Actions run page."""
    fake_mftool(FakeMftool())
    monkeypatch.setattr(main_pipeline.config, "PERFORMANCE_REPORT_FILE", tmp_path / "legacy.csv")
    summary = tmp_path / "summary.md"
    main_pipeline.main(
        [
            "--schemes",
            "111111",
            "--benchmark",
            "none",
            "--db-path",
            str(tmp_path / "s.db"),
            "--output-dir",
            str(tmp_path / "r"),
            "--summary-file",
            str(summary),
        ]
    )
    text = summary.read_text()
    assert "Executive summary" in text
    assert "Methodology & assumptions" in text


def test_pipeline_skip_fetch_reuses_stored_data(populated_db, tmp_path, monkeypatch):
    monkeypatch.setattr(main_pipeline.config, "PERFORMANCE_REPORT_FILE", tmp_path / "legacy.csv")
    exit_code = main_pipeline.main(
        [
            "--skip-fetch",
            "--schemes",
            "111111",
            "222222",
            "--benchmark",
            "none",
            "--db-path",
            str(populated_db),
            "--output-dir",
            str(tmp_path / "r"),
        ]
    )
    assert exit_code == main_pipeline.EXIT_OK


def test_benchmark_none_disables_relative_metrics(populated_db, tmp_path, monkeypatch):
    monkeypatch.setattr(main_pipeline.config, "PERFORMANCE_REPORT_FILE", tmp_path / "legacy.csv")
    args = main_pipeline.build_parser().parse_args(
        [
            "--benchmark",
            "NONE",
            "--schemes",
            "111111",
            "--skip-fetch",
            "--db-path",
            str(populated_db),
            "--output-dir",
            str(tmp_path / "r"),
        ]
    )
    assert main_pipeline.run_pipeline(args) == main_pipeline.EXIT_OK


def test_summary_write_failure_does_not_fail_the_run(tmp_path, caplog):
    """A broken summary path is a reporting nuisance, not an analysis failure."""
    main_pipeline._append_summary(tmp_path / "nonexistent\0dir" / "f.md", "# hi")
    # The call must simply not raise; the log message is best-effort.
    assert True
