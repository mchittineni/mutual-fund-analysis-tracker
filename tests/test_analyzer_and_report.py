"""End-to-end tests: analysis orchestration, insight generation, and report rendering."""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from src import analyzer, config, db_manager, report, validation

# --- analysis --------------------------------------------------------------


def test_analyse_produces_metrics_for_every_healthy_scheme(populated_db):
    result = analyzer.analyse(populated_db, scheme_codes=["111111", "222222"], benchmark_code=None)
    assert {s.scheme_code for s in result.schemes} == {"111111", "222222"}
    assert result.as_of is not None
    assert result.insights
    assert not result.has_synthetic_data


def test_analyse_excludes_schemes_that_fail_the_quality_gate(populated_db):
    """A scheme with too little history is dropped from metrics but stays visible in findings."""
    conn = db_manager.setup_database(populated_db)
    db_manager.upsert_scheme_info(conn, "333333", "Tiny Fund", "AMC", "Open", "Equity", "amfi")
    db_manager.upsert_nav_records(
        conn, [("333333", f"2024-01-{day:02d}", 100.0 + day) for day in range(1, 6)], "amfi"
    )
    conn.close()

    result = analyzer.analyse(populated_db, scheme_codes=["111111", "333333"], benchmark_code=None)
    assert {s.scheme_code for s in result.schemes} == {"111111"}
    assert "333333" in result.quality.unusable_schemes()
    assert any(i.category == "data-integrity" for i in result.insights)


def test_analyse_reports_missing_scheme_as_critical(populated_db):
    result = analyzer.analyse(populated_db, scheme_codes=["111111", "999999"], benchmark_code=None)
    assert "999999" in result.quality.unusable_schemes()
    assert not any(s.scheme_code == "999999" for s in result.schemes)


def test_analyse_computes_benchmark_relative_metrics(populated_db):
    result = analyzer.analyse(populated_db, scheme_codes=["222222"], benchmark_code="111111")
    scheme = result.schemes[0]
    assert scheme.benchmark is not None
    assert scheme.benchmark.benchmark_code == "111111"
    assert scheme.benchmark.overlap_days > 0
    assert result.benchmark_name is not None


def test_benchmark_is_not_reported_as_a_holding(populated_db):
    """The benchmark is loaded for comparison but must not appear as an analysed scheme."""
    result = analyzer.analyse(populated_db, scheme_codes=["222222"], benchmark_code="111111")
    assert [s.scheme_code for s in result.schemes] == ["222222"]
    assert "111111" in result.nav_series  # still available for charting


def test_analyse_on_an_empty_database_returns_an_empty_result(db_path):
    db_manager.setup_database(db_path).close()
    result = analyzer.analyse(db_path, scheme_codes=["111111"])
    assert result.schemes == []
    assert result.quality.has_critical
    assert result.to_frame().empty


def test_risk_free_rate_flows_through_to_sharpe(populated_db):
    low = analyzer.analyse(populated_db, ["111111"], benchmark_code=None, risk_free_rate=0.0)
    high = analyzer.analyse(populated_db, ["111111"], benchmark_code=None, risk_free_rate=0.30)
    # A higher hurdle must lower the Sharpe ratio -- the assumption is not cosmetic.
    assert low.schemes[0].sharpe_ratio > high.schemes[0].sharpe_ratio
    assert high.assumptions["risk_free_rate_annual_pct"] == 30.0


def test_synthetic_data_is_surfaced_in_the_analysis(db_path):
    conn = db_manager.setup_database(db_path)
    from src import fetch_amfi_data

    fetch_amfi_data.generate_synthetic_history(conn, ["119598"], years=4.0)
    conn.close()
    result = analyzer.analyse(db_path, scheme_codes=["119598"], benchmark_code=None)
    assert result.has_synthetic_data
    assert result.insights[0].category == "data-integrity"
    assert "SYNTHETIC" in result.insights[0].headline


def test_technical_indicators_leave_early_rows_null(nav_frame):
    """A 50-day SMA must not be computed from 12 observations."""
    frame = analyzer.add_technical_indicators(nav_frame)
    first = frame.groupby("scheme_code").head(49)
    assert first["50D_SMA"].isna().all()
    long_scheme = frame[frame["scheme_code"] == "111111"]
    assert long_scheme["50D_SMA"].notna().any()


def test_indicators_do_not_bleed_across_schemes(nav_frame):
    frame = analyzer.add_technical_indicators(nav_frame)
    for _code, group in frame.groupby("scheme_code"):
        # The first 49 rows of *each* scheme must be NaN, proving per-group windows.
        assert group["50D_SMA"].iloc[:49].isna().all()


def test_ranked_by_orders_and_drops_missing_values(populated_db):
    result = analyzer.analyse(populated_db, ["111111", "222222"], benchmark_code=None)
    ranked = result.ranked_by("cagr_since_inception_pct")
    values = [s.cagr_since_inception_pct for s in ranked]
    assert values == sorted(values, reverse=True)


def test_generate_weekly_report_writes_the_legacy_csv(populated_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PERFORMANCE_REPORT_FILE", tmp_path / "perf.csv")
    monkeypatch.setattr(config, "DEFAULT_TARGET_SCHEMES", ["111111", "222222"])
    frame = analyzer.generate_weekly_report(populated_db)
    assert not frame.empty
    assert (tmp_path / "perf.csv").exists()


# --- insights --------------------------------------------------------------


def test_insights_lead_with_data_integrity_over_performance():
    quality = validation.QualityReport()
    quality.add(validation.Finding("1", "CRITICAL", "no_data", "nothing stored"))
    insights = analyzer.derive_insights([], quality)
    assert insights[0].category == "data-integrity"


def test_insights_are_empty_only_when_there_is_nothing_to_say():
    assert analyzer.derive_insights([], validation.QualityReport()) == []


def test_insights_rank_on_risk_adjusted_return_not_raw_return(populated_db):
    result = analyzer.analyse(populated_db, ["111111", "222222"], benchmark_code=None)
    text = " ".join(f"{i.headline} {i.detail}" for i in result.insights)
    assert "risk-adjusted" in text
    # The Sharpe-based finding must precede the raw-CAGR finding.
    performance = [i for i in result.insights if i.category == "performance"]
    assert "risk-adjusted" in performance[0].headline


def test_insights_flag_a_stale_feed(nav_frame, db_path):
    """Insights must surface staleness, not bury it in the quality table."""
    conn = db_manager.setup_database(db_path)
    db_manager.upsert_scheme_info(conn, "111111", "Old Fund", "AMC", "Open", "Equity", "amfi")
    index = pd.bdate_range(end="2024-01-31", periods=400)
    db_manager.upsert_nav_records(
        conn,
        [(("111111"), d.strftime("%Y-%m-%d"), 100.0 + i * 0.05) for i, d in enumerate(index)],
        "amfi",
    )
    conn.close()
    result = analyzer.analyse(db_path, ["111111"], benchmark_code=None)
    assert any("Stale" in i.headline for i in result.insights)


# --- report rendering ------------------------------------------------------


def test_markdown_contains_every_required_section(populated_db):
    result = analyzer.analyse(populated_db, ["111111", "222222"], benchmark_code=None)
    markdown = report.render_markdown(result)
    for heading in (
        "# " + config.REPORT_TITLE,
        "## Executive summary",
        "## Performance",
        "## Risk & risk-adjusted return",
        "## Rolling 3-year return consistency",
        "## Trend (moving averages)",
        "## Data quality",
        "## Methodology & assumptions",
    ):
        assert heading in markdown
    assert config.DISCLAIMER in markdown


def test_markdown_renders_missing_values_as_a_dash_not_zero(db_path):
    """A missing metric must never render as 0.00 -- that reads as a real measurement."""
    conn = db_manager.setup_database(db_path)
    db_manager.upsert_scheme_info(conn, "111111", "Young Fund", "AMC", "Open", "Equity", "amfi")
    index = pd.bdate_range(end=pd.Timestamp.today(), periods=60)
    db_manager.upsert_nav_records(
        conn,
        [("111111", d.strftime("%Y-%m-%d"), 100.0 + i * 0.1) for i, d in enumerate(index)],
        "amfi",
    )
    conn.close()
    result = analyzer.analyse(db_path, ["111111"], benchmark_code=None)
    markdown = report.render_markdown(result)
    assert "—" in markdown  # 3Y/5Y columns are unavailable and shown as em dashes


def test_markdown_shouts_about_synthetic_data(db_path):
    conn = db_manager.setup_database(db_path)
    from src import fetch_amfi_data

    fetch_amfi_data.generate_synthetic_history(conn, ["119598"], years=4.0)
    conn.close()
    result = analyzer.analyse(db_path, ["119598"], benchmark_code=None)
    markdown = report.render_markdown(result)
    assert "[!CAUTION]" in markdown
    assert "SYNTHETIC" in markdown


def test_html_is_self_contained_and_theme_aware(populated_db):
    result = analyzer.analyse(populated_db, ["111111", "222222"], benchmark_code=None)
    page = report.render_html(result)
    assert page.startswith("<!doctype html>")
    # No external requests: a strict CSP or an offline reader must still see the report.
    for forbidden in ("http://", "https://", "<script"):
        assert forbidden not in page
    assert "prefers-color-scheme" in page
    assert "<svg" in page  # charts are inline SVG


def test_html_escapes_scheme_names(db_path):
    conn = db_manager.setup_database(db_path)
    db_manager.upsert_scheme_info(
        conn, "111111", "<script>alert(1)</script> Fund", "AMC", "Open", "Equity", "amfi"
    )
    index = pd.bdate_range(end=pd.Timestamp.today(), periods=300)
    db_manager.upsert_nav_records(
        conn,
        [("111111", d.strftime("%Y-%m-%d"), 100.0 + i * 0.1) for i, d in enumerate(index)],
        "amfi",
    )
    conn.close()
    result = analyzer.analyse(db_path, ["111111"], benchmark_code=None)
    page = report.render_html(result)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_json_is_valid_and_carries_the_full_contract(populated_db):
    result = analyzer.analyse(populated_db, ["111111", "222222"], benchmark_code=None)
    payload = json.loads(report.render_json(result))
    assert payload["schemes"] and payload["insights"]
    assert set(payload) >= {
        "generated_at",
        "as_of",
        "benchmark",
        "assumptions",
        "contains_synthetic_data",
        "schemes",
        "insights",
        "data_quality",
    }
    assert payload["data_quality"]["summary"]["schemes_checked"] >= 2
    # Dates must serialise as strings, not Python reprs.
    assert isinstance(payload["schemes"][0]["as_of"], str)


def test_write_reports_creates_all_artifacts(populated_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PERFORMANCE_REPORT_FILE", tmp_path / "legacy.csv")
    result = analyzer.analyse(populated_db, ["111111", "222222"], benchmark_code=None)
    paths = report.write_reports(result, tmp_path / "out")
    assert set(paths) == {"markdown", "html", "json", "csv"}
    for path in paths.values():
        assert path.exists() and path.stat().st_size > 0
    assert paths["html"].name == "index.html"  # GitHub Pages entry point


def test_reports_render_for_an_empty_analysis(db_path, tmp_path):
    db_manager.setup_database(db_path).close()
    result = analyzer.analyse(db_path, ["111111"])
    # Rendering must not crash when there is nothing to report.
    assert "Executive summary" in report.render_markdown(result)
    assert report.render_html(result).startswith("<!doctype html>")
    assert json.loads(report.render_json(result))["schemes"] == []
    paths = report.write_reports(result, tmp_path / "empty")
    assert "csv" not in paths


def test_console_output_summarises_without_crashing(populated_db):
    result = analyzer.analyse(populated_db, ["111111", "222222"], benchmark_code=None)
    text = report.render_console(result)
    assert config.REPORT_TITLE in text
    assert "Steady Growth Fund" in text


def test_svg_chart_handles_an_empty_series_map():
    assert "No data available" in report._svg_line_chart({}, title="Empty", y_label="y")


def test_svg_chart_decimates_long_series():
    index = pd.bdate_range("2000-01-01", periods=5000)
    series = pd.Series(range(5000), index=index, dtype=float)
    svg = report._svg_line_chart({"Long": series}, title="Long", y_label="y")
    # Decimation keeps the file small; ~700 points plus the final one.
    assert svg.count(",") < 900
    assert "<polyline" in svg
