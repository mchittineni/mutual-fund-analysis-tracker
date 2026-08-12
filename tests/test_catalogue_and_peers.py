"""
Tests for full-universe catalogue ingestion and category-relative ranking.

The catalogue parser is the piece with the most external risk in this codebase:
it reads a positional, semicolon-delimited file that AMFI can change without
notice, and everything downstream -- categories, fund houses, peer sets --
depends on it. So these tests pin the *structural* rules rather than the sample
values: that a section header sets the category for the lines beneath it, that a
bare line names the AMC, and that junk is counted rather than raised on.

Nothing here touches the network. `fixtures/navall_sample.txt` is a hand-built
excerpt in AMFI's documented format, including the awkward parts (a missing NAV,
a short line, a duplicate scheme, a free-text notice).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src import catalogue, config, db_manager, peers

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "navall_sample.txt"


@pytest.fixture
def navall_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parser_attributes_category_and_fund_house_from_position(navall_text):
    """Category comes from the section header, fund house from the bare line above."""
    entries, _ = catalogue.parse_navall(navall_text)
    by_code = {entry.scheme_code: entry for entry in entries}

    sbi = by_code["119598"]
    assert sbi.scheme_name == "SBI Blue Chip Fund - Direct Plan - Growth"
    assert sbi.fund_house == "SBI Mutual Fund"
    assert sbi.scheme_category == "Equity Scheme - Large Cap Fund"
    assert sbi.scheme_type == "Open Ended Schemes"
    assert sbi.isin_growth == "INF200K01QX4"
    assert sbi.isin_reinvest == "INF200K01QY2"
    assert sbi.nav == pytest.approx(104.8)
    assert sbi.nav_date == "2026-08-12"

    # Same section, a different AMC named partway through.
    assert by_code["118989"].fund_house == "HDFC Mutual Fund"
    assert by_code["118989"].scheme_category == "Equity Scheme - Large Cap Fund"

    # A later section changes the category for everything beneath it -- including
    # for an AMC that already appeared under the previous category.
    assert by_code["119063"].fund_house == "HDFC Mutual Fund"
    assert by_code["119063"].scheme_category == "Debt Scheme - Liquid Fund"
    assert by_code["112345"].scheme_type == "Close Ended Schemes"
    assert by_code["112345"].scheme_category == "Income"


def test_parser_skips_junk_without_losing_the_file(navall_text):
    """One malformed line must cost one scheme, never the other 14,000."""
    entries, skipped = catalogue.parse_navall(navall_text)

    # The column header, the too-short line, and the repeated scheme.
    assert skipped == 3
    assert len(entries) == 7
    assert len({entry.scheme_code for entry in entries}) == len(entries)
    assert all(entry.scheme_code.isdigit() for entry in entries)


def test_missing_nav_is_none_not_zero(navall_text):
    """AMFI writes 'N.A.' for a scheme yet to publish. Zero would be a real price."""
    entries, _ = catalogue.parse_navall(navall_text)
    pending = next(e for e in entries if e.scheme_code == "100001")
    assert pending.nav is None
    assert pending.isin_growth is None  # '-' means absent, not a literal hyphen
    assert pending.scheme_name == "A Scheme With No NAV Yet"


def test_parsing_an_empty_or_unrecognised_file_yields_nothing():
    """A format change must surface as zero entries, which the caller treats as an error."""
    entries, _ = catalogue.parse_navall("")
    assert entries == []
    entries, skipped = catalogue.parse_navall("something entirely unexpected\n")
    assert entries == []
    assert skipped == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12-Aug-2026", "2026-08-12"),
        ("01-January-2025", "2025-01-01"),
        ("2026-08-12", "2026-08-12"),
        ("not a date", None),
        ("-", None),
    ],
)
def test_date_parsing_accepts_amfi_spellings(raw, expected):
    assert catalogue._parse_date(raw) == expected


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_refresh_stores_metadata_and_the_days_nav(conn, navall_text):
    result = catalogue.refresh_catalogue(conn, text=navall_text)

    assert result.parsed == 7
    assert result.categories == 3
    assert result.fund_houses == 4
    assert result.nav_date == "2026-08-12"
    # Six schemes published a NAV; the seventh is still 'N.A.'.
    assert result.navs_written == 6
    assert not result.errors

    stored = conn.execute(
        "SELECT fund_house, scheme_category FROM scheme_info WHERE scheme_code = '119063'"
    ).fetchone()
    assert stored["fund_house"] == "HDFC Mutual Fund"
    assert stored["scheme_category"] == "Debt Scheme - Liquid Fund"


def test_refresh_is_idempotent(conn, navall_text):
    """Re-running the same day's file must not duplicate schemes or NAV rows."""
    catalogue.refresh_catalogue(conn, text=navall_text)
    first = conn.execute("SELECT COUNT(*) FROM nav_history").fetchone()[0]

    catalogue.refresh_catalogue(conn, text=navall_text)
    second = conn.execute("SELECT COUNT(*) FROM nav_history").fetchone()[0]

    assert first == second == 6
    assert conn.execute("SELECT COUNT(*) FROM scheme_info").fetchone()[0] == 7


def test_refresh_reports_a_format_change_as_an_error(conn):
    """Zero parsed schemes is the signature of a changed feed, and must be loud."""
    result = catalogue.refresh_catalogue(conn, text="AMFI has redesigned this file\n")
    assert result.parsed == 0
    assert result.errors
    assert "format" in result.errors[0].lower()


def test_catalogue_refresh_does_not_blank_richer_existing_metadata(conn, navall_text):
    """A per-scheme detail fetch enriches a row; the next catalogue run must not undo it."""
    catalogue.refresh_catalogue(conn, text=navall_text)
    conn.execute("UPDATE scheme_info SET fund_house = 'Enriched AMC' WHERE scheme_code = '100001'")
    conn.commit()

    # The fixture gives 100001 a fund house, so a refresh legitimately overwrites
    # it. A scheme whose incoming fund house is NULL is the case that must survive.
    entry = catalogue.CatalogueEntry(
        scheme_code="100001",
        scheme_name="A Scheme With No NAV Yet",
        fund_house=None,
        scheme_type=None,
        scheme_category=None,
    )
    db_manager.upsert_catalogue(conn, [entry])
    row = conn.execute(
        "SELECT fund_house, scheme_category FROM scheme_info WHERE scheme_code = '100001'"
    ).fetchone()
    assert row["fund_house"] == "Enriched AMC"
    assert row["scheme_category"] == "Debt Scheme - Liquid Fund"


def test_catalogue_stats_survives_a_database_that_does_not_exist(tmp_path):
    """The scheduled job probes coverage before anything has been written."""
    stats = db_manager.catalogue_stats(tmp_path / "not-created-yet.db")
    assert stats == {
        "schemes": 0,
        "categories": 0,
        "fund_houses": 0,
        "analysable": 0,
        "nav_rows": 0,
    }


def test_search_finds_schemes_by_name_category_and_house(conn, db_path, navall_text):
    catalogue.refresh_catalogue(conn, text=navall_text)
    conn.close()

    by_name = db_manager.search_schemes(db_path, query="Blue Chip")
    assert len(by_name) == 2
    assert set(by_name["scheme_code"]) == {"119598", "119599"}

    by_category = db_manager.search_schemes(db_path, category="Debt Scheme - Liquid Fund")
    assert set(by_category["scheme_code"]) == {"119063", "120716", "100001"}

    by_house = db_manager.search_schemes(db_path, fund_house="HDFC Mutual Fund")
    assert set(by_house["scheme_code"]) == {"118989", "119063"}

    # One NAV apiece is nowhere near analysable, so this must return nothing.
    assert db_manager.search_schemes(db_path, with_history_only=True).empty


# ---------------------------------------------------------------------------
# The backfill queue
# ---------------------------------------------------------------------------


def test_backfill_queue_holds_only_schemes_that_need_history(conn, navall_text):
    catalogue.refresh_catalogue(conn, text=navall_text)

    # Give one scheme a full history; it should drop out of the queue.
    dates = pd.bdate_range(end="2026-08-12", periods=config.MIN_OBSERVATIONS + 10)
    db_manager.upsert_nav_records(
        conn,
        [("119598", d.strftime("%Y-%m-%d"), 100.0 + i) for i, d in enumerate(dates)],
        "amfi",
    )

    queue = catalogue.schemes_needing_history(conn, limit=50)
    assert "119598" not in queue
    assert set(queue) == {"119599", "118989", "119063", "120716", "100001", "112345"}


def test_backfill_queue_respects_filters_and_limit(conn, navall_text):
    catalogue.refresh_catalogue(conn, text=navall_text)

    liquid = catalogue.schemes_needing_history(conn, category="Debt Scheme - Liquid Fund", limit=50)
    assert set(liquid) == {"119063", "120716", "100001"}

    hdfc = catalogue.schemes_needing_history(conn, fund_house="HDFC Mutual Fund", limit=50)
    assert set(hdfc) == {"118989", "119063"}

    assert len(catalogue.schemes_needing_history(conn, limit=2)) == 2


def test_backfill_stops_at_the_time_budget(conn, monkeypatch, navall_text):
    """The budget is what keeps a 14,000-request job inside a runner timeout."""
    catalogue.refresh_catalogue(conn, text=navall_text)
    monkeypatch.setattr(catalogue.fetch_amfi_data, "MFTOOL_AVAILABLE", True)
    monkeypatch.setattr(catalogue.fetch_amfi_data, "amfi_reachable", lambda *_a, **_k: True)
    monkeypatch.setattr(catalogue.fetch_amfi_data, "Mftool", lambda *_a, **_k: object())

    fetched: list[str] = []
    clock = iter([0.0] + [float(n) for n in range(1, 200)])

    def fake_fetch(_mf, code, *_a, **_k):
        fetched.append(code)
        return ({"scheme_code": code, "scheme_name": f"Scheme {code}"}, [])

    monkeypatch.setattr(catalogue.fetch_amfi_data, "fetch_scheme", fake_fetch)
    monkeypatch.setattr(catalogue.time, "monotonic", lambda: next(clock))

    queue = catalogue.schemes_needing_history(conn, limit=50)
    result = catalogue.backfill_history(conn, queue, polite_delay=0, time_budget_seconds=2)

    # The clock advances a second per loop check, so only the schemes reached
    # before the budget expired were fetched -- the rest are left for next time.
    assert 0 < len(fetched) < len(queue)
    assert len(result.succeeded) == len(fetched)


def test_backfill_records_a_failure_without_abandoning_the_run(conn, monkeypatch, navall_text):
    catalogue.refresh_catalogue(conn, text=navall_text)
    monkeypatch.setattr(catalogue.fetch_amfi_data, "MFTOOL_AVAILABLE", True)
    monkeypatch.setattr(catalogue.fetch_amfi_data, "amfi_reachable", lambda *_a, **_k: True)
    monkeypatch.setattr(catalogue.fetch_amfi_data, "Mftool", lambda *_a, **_k: object())

    def flaky(_mf, code, *_a, **_k):
        if code == "119599":
            raise RuntimeError("AMFI returned nothing")
        return ({"scheme_code": code, "scheme_name": f"Scheme {code}"}, [])

    monkeypatch.setattr(catalogue.fetch_amfi_data, "fetch_scheme", flaky)

    result = catalogue.backfill_history(conn, ["119598", "119599", "118989"], polite_delay=0)
    assert result.succeeded == ["119598", "118989"]
    assert "119599" in result.failed

    run = conn.execute("SELECT status FROM ingestion_runs ORDER BY run_id DESC LIMIT 1").fetchone()
    assert run["status"] == "partial"


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


def test_cli_ingests_from_a_file_and_writes_a_summary(tmp_path, monkeypatch):
    """`--from-file` is the offline path the tests and a replay both use."""
    db = tmp_path / "cli.db"
    summary = tmp_path / "summary.md"
    monkeypatch.setattr(config, "DB_PATH", db)

    exit_code = catalogue.main(
        [
            "--db-path",
            str(db),
            "--from-file",
            str(FIXTURE),
            "--summary-file",
            str(summary),
            "--log-level",
            "ERROR",
        ]
    )

    assert exit_code == 0
    text = summary.read_text(encoding="utf-8")
    assert "AMFI catalogue refresh" in text
    assert "Coverage by category" in text
    assert "Debt Scheme - Liquid Fund" in text
    # No backfill was requested, so that section must be absent rather than empty.
    assert "History backfill" not in text


def test_cli_returns_two_when_the_feed_cannot_be_parsed(tmp_path, monkeypatch):
    """Exit 2 means ingestion failed -- distinct from exit 1, an unexpected error."""
    unusable = tmp_path / "changed.txt"
    unusable.write_text("AMFI has redesigned this file\n", encoding="utf-8")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "cli.db")

    assert (
        catalogue.main(
            [
                "--db-path",
                str(tmp_path / "cli.db"),
                "--from-file",
                str(unusable),
                "--log-level",
                "ERROR",
            ]
        )
        == 2
    )


# ---------------------------------------------------------------------------
# Peer ranking
# ---------------------------------------------------------------------------


def _peer_frame(values: dict[str, dict[str, float]]) -> pd.DataFrame:
    return pd.DataFrame([{"scheme_code": code, **metrics} for code, metrics in values.items()])


def test_percentile_uses_mid_rank_for_ties():
    """Ten identical funds all sit at the 50th percentile, not all at the 100th."""
    identical = pd.Series([5.0] * 10)
    assert peers.percentile_of(identical, 5.0) == pytest.approx(50.0)

    spread = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    assert peers.percentile_of(spread, 5.0) == pytest.approx(90.0)
    assert peers.percentile_of(spread, 1.0) == pytest.approx(10.0)
    assert peers.percentile_of(spread, 3.0) == pytest.approx(50.0)


def test_percentile_is_inverted_when_lower_is_better():
    """The 90th percentile must always mean 'good', whichever way the metric runs."""
    volatility = pd.Series([10.0, 12.0, 14.0, 16.0, 20.0])
    # The calmest fund is the best fund.
    assert peers.percentile_of(volatility, 10.0, higher_is_better=False) == pytest.approx(90.0)
    assert peers.percentile_of(volatility, 20.0, higher_is_better=False) == pytest.approx(10.0)


def test_percentile_of_an_empty_peer_set_is_nan():
    assert np.isnan(peers.percentile_of(pd.Series([], dtype=float), 5.0))


def test_rank_within_refuses_a_peer_set_that_is_too_small():
    """A percentile against three funds is noise dressed as precision."""
    frame = _peer_frame(
        {"A": {"sharpe_ratio": 1.0}, "B": {"sharpe_ratio": 0.5}, "C": {"sharpe_ratio": 0.2}}
    )
    assert peers.rank_within(frame, "A", "sharpe_ratio") is None


def test_rank_within_reports_position_median_and_quartile():
    frame = _peer_frame(
        {str(i): {"cagr_3y_pct": float(v)} for i, v in enumerate([4, 8, 12, 16, 20, 24])}
    )
    best = peers.rank_within(frame, "5", "cagr_3y_pct")
    assert best is not None
    assert best.rank == 1
    assert best.peers == 6
    assert best.quartile == 1
    assert best.category_median == pytest.approx(14.0)
    assert best.higher_is_better

    worst = peers.rank_within(frame, "0", "cagr_3y_pct")
    assert worst is not None
    assert worst.rank == 6
    assert worst.quartile == 4


def test_lower_is_better_metrics_rank_the_calm_fund_first():
    """A volatility ranking that put the wildest fund in Q1 would be actively harmful."""
    frame = _peer_frame(
        {str(i): {"volatility_pct": float(v)} for i, v in enumerate([8, 12, 16, 20, 24, 28])}
    )
    comparison = peers.compare_within_category(frame, "0", "Equity Scheme - Large Cap Fund")
    assert comparison is not None
    calm = comparison.ranks["volatility_pct"]
    assert calm.rank == 1
    assert calm.quartile == 1
    assert not calm.higher_is_better

    wild = peers.compare_within_category(frame, "5", "Equity Scheme - Large Cap Fund")
    assert wild is not None
    assert wild.ranks["volatility_pct"].quartile == 4


def test_comparison_separates_strengths_from_weaknesses():
    """The whole point of per-metric percentiles: a fund can be Q1 and Q4 at once."""
    rows = {}
    for i, (ret, vol) in enumerate([(4, 8), (8, 10), (12, 12), (16, 14), (20, 16), (30, 30)]):
        rows[str(i)] = {"cagr_3y_pct": float(ret), "volatility_pct": float(vol)}
    frame = _peer_frame(rows)

    comparison = peers.compare_within_category(frame, "5", "Equity")
    assert comparison is not None
    assert "cagr_3y_pct" in comparison.top_quartile_metrics()
    assert "volatility_pct" in comparison.bottom_quartile_metrics()

    row = comparison.as_row()
    assert row["peer_category"] == "Equity"
    assert row["pct_cagr_3y_pct"] > row["pct_volatility_pct"]


def test_metrics_missing_for_a_scheme_are_omitted_not_guessed():
    """A fund without three years of history has no 3y percentile. Not a zero."""
    frame = _peer_frame(
        {str(i): {"cagr_3y_pct": float(i), "sharpe_ratio": float(i) / 2} for i in range(6)}
    )
    frame.loc[frame["scheme_code"] == "3", "cagr_3y_pct"] = np.nan

    comparison = peers.compare_within_category(frame, "3", "Equity")
    assert comparison is not None
    assert "cagr_3y_pct" not in comparison.ranks
    assert "sharpe_ratio" in comparison.ranks


def test_peer_comparison_over_the_database(populated_db, monkeypatch):
    """End to end: too few peers in the fixture category, so no percentile is reported."""
    monkeypatch.setattr(config, "DB_PATH", populated_db)
    assert peers.peer_comparison(populated_db, scheme_code="111111", category="Equity") is None


def test_category_summary_picks_the_best_end_of_each_metric():
    frame = _peer_frame(
        {
            "A": {"cagr_3y_pct": 10.0, "volatility_pct": 20.0},
            "B": {"cagr_3y_pct": 20.0, "volatility_pct": 10.0},
            "C": {"cagr_3y_pct": 30.0, "volatility_pct": 30.0},
        }
    )
    summary = peers.category_summary(frame)
    assert summary["schemes"] == 3
    assert summary["cagr_3y_pct_median"] == pytest.approx(20.0)
    # Highest return is best...
    assert summary["cagr_3y_pct_best"] == pytest.approx(30.0)
    # ...but lowest volatility is.
    assert summary["volatility_pct_best"] == pytest.approx(10.0)


def test_category_summary_of_an_empty_frame_is_empty():
    assert peers.category_summary(pd.DataFrame()) == {}


# ---------------------------------------------------------------------------
# Pipeline universe selection
# ---------------------------------------------------------------------------


def _pipeline_args(**overrides):
    import main_pipeline

    defaults = {
        "schemes": None,
        "all_analysable": False,
        "category": None,
        "fund_house": None,
        "max_schemes": 100,
        "db_path": None,
        "skip_fetch": False,
    }
    defaults.update(overrides)
    return main_pipeline.argparse.Namespace(**defaults)


def _seed_analysable(conn, codes, category="Equity Scheme - Large Cap Fund"):
    """Give each code enough NAV history to clear MIN_OBSERVATIONS."""
    dates = pd.bdate_range(end="2026-08-12", periods=config.MIN_OBSERVATIONS + 5)
    for code in codes:
        db_manager.upsert_scheme_info(
            conn, code, f"Fund {code}", "AMC A", "Open Ended Schemes", category, "amfi"
        )
        db_manager.upsert_nav_records(
            conn,
            [(code, d.strftime("%Y-%m-%d"), 100.0 + i) for i, d in enumerate(dates)],
            "amfi",
        )


def test_all_analysable_uses_the_database_and_skips_fetching(conn, db_path):
    """The catalogue fills the database; --all-analysable is what analyses it.

    Fetching is skipped because full history is one request per scheme -- with a
    few hundred schemes that would take longer than the analysis, for data
    already stored.
    """
    import main_pipeline

    _seed_analysable(conn, [f"20000{i}" for i in range(6)])
    conn.close()

    codes, skip_fetch = main_pipeline.resolve_universe(
        _pipeline_args(all_analysable=True, db_path=db_path)
    )
    assert len(codes) == 6
    assert skip_fetch is True


def test_explicit_schemes_still_win(conn, db_path):
    import main_pipeline

    _seed_analysable(conn, ["200001", "200002"])
    conn.close()
    codes, _ = main_pipeline.resolve_universe(
        _pipeline_args(schemes=["999999"], all_analysable=True, db_path=db_path)
    )
    assert codes == ["999999"]


def test_category_filter_narrows_the_universe(conn, db_path):
    import main_pipeline

    _seed_analysable(conn, ["300001", "300002"], category="Debt Scheme - Liquid Fund")
    _seed_analysable(conn, ["300003"], category="Equity Scheme - Large Cap Fund")
    conn.close()

    codes, _ = main_pipeline.resolve_universe(
        _pipeline_args(category="Debt Scheme - Liquid Fund", db_path=db_path)
    )
    assert set(codes) == {"300001", "300002"}


def test_max_schemes_caps_the_universe(conn, db_path):
    import main_pipeline

    _seed_analysable(conn, [f"40000{i}" for i in range(8)])
    conn.close()
    codes, _ = main_pipeline.resolve_universe(
        _pipeline_args(all_analysable=True, max_schemes=3, db_path=db_path)
    )
    assert len(codes) == 3


def test_a_filter_matching_nothing_stops_rather_than_analysing_something_else(conn, db_path):
    """Answering a different question than the one asked is worse than failing."""
    import main_pipeline

    _seed_analysable(conn, ["500001"])
    conn.close()
    codes, _ = main_pipeline.resolve_universe(
        _pipeline_args(category="Nonexistent Category", db_path=db_path)
    )
    assert codes == []


def test_all_analysable_falls_back_loudly_on_an_empty_database(conn, db_path, caplog):
    """A cache eviction must not stop the scheduled report publishing -- but the
    smaller universe has to be visible in the log, not silent."""
    import main_pipeline

    conn.close()
    with caplog.at_level("WARNING"):
        codes, _ = main_pipeline.resolve_universe(
            _pipeline_args(all_analysable=True, db_path=db_path)
        )
    assert codes == [str(c) for c in config.DEFAULT_TARGET_SCHEMES]
    assert "falling back" in caplog.text.lower()


def test_backfill_queue_prefers_open_ended_schemes(conn):
    """~4,700 of AMFI's entries are close-ended, mostly matured FMPs. Filling
    those first would waste weeks of runs on funds nobody can buy."""
    for code, scheme_type in (
        ("600001", "Close Ended Schemes"),
        ("600002", "Open Ended Schemes"),
        ("600003", "Close Ended Schemes"),
        ("600004", "Open Ended Schemes"),
        ("600005", "Interval Fund Schemes"),
    ):
        db_manager.upsert_scheme_info(
            conn, code, f"Fund {code}", "AMC A", scheme_type, "Income", "amfi"
        )

    queue = catalogue.schemes_needing_history(conn, limit=10)
    types = [
        conn.execute(
            "SELECT scheme_type FROM scheme_info WHERE scheme_code = ?", (code,)
        ).fetchone()[0]
        for code in queue
    ]
    assert types[:2] == ["Open Ended Schemes", "Open Ended Schemes"]
    assert types[2] == "Interval Fund Schemes"
    assert types[3:] == ["Close Ended Schemes", "Close Ended Schemes"]
