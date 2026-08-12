"""
Tests for screening and composite scoring.

A composite score is the easiest thing in this project to get quietly wrong: it
looks authoritative whatever it does. So most of these tests are about what the
score must *refuse* to do -- score a fund against four peers, penalise a young
fund for a metric it cannot have yet, or rank a liquid fund against a small-cap
one -- rather than about the arithmetic, which is a weighted mean.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import peers, screener


def _category_frame(
    count: int = 8, category: str = "Equity Scheme - Large Cap Fund"
) -> pd.DataFrame:
    """`count` funds, monotonically better as the index rises."""
    return pd.DataFrame(
        [
            {
                "scheme_code": f"20000{i}",
                "scheme_name": f"Fund {i}",
                "scheme_category": category,
                "fund_house": "AMC A" if i % 2 else "AMC B",
                "return_1y_pct": 5.0 + i,
                "cagr_3y_pct": 6.0 + i,
                "cagr_5y_pct": 7.0 + i,
                "sharpe_ratio": 0.2 + i * 0.1,
                "sortino_ratio": 0.3 + i * 0.1,
                "calmar_ratio": 0.4 + i * 0.1,
                "max_drawdown_pct": -30.0 + i,
                "volatility_pct": 25.0 - i,
                "sip_xirr_3y_pct": 5.0 + i,
                "observations": 800,
                "history_years": 3.2,
            }
            for i in range(count)
        ]
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_score_ranks_the_best_fund_first_and_ships_its_components():
    scored = screener.score_frame(_category_frame())

    best = scored.loc[scored["scheme_code"] == "200007"].iloc[0]
    worst = scored.loc[scored["scheme_code"] == "200000"].iloc[0]
    assert best["score"] > worst["score"]
    assert 0 <= worst["score"] <= best["score"] <= 100

    # The components are the whole point: a bare score is not shippable.
    for component in screener.COMPONENT_METRICS:
        assert f"score_{component}" in scored.columns
        assert pd.notna(best[f"score_{component}"])
    assert best["score_components"] == len(screener.COMPONENT_METRICS)


def test_low_volatility_lifts_the_score_rather_than_lowering_it():
    """The drawdown component must reward calm funds, or the score is inverted."""
    frame = _category_frame()
    scored = screener.score_frame(frame)
    calmest = scored.loc[scored["volatility_pct"].idxmin()]
    wildest = scored.loc[scored["volatility_pct"].idxmax()]
    assert calmest["score_drawdown"] > wildest["score_drawdown"]


def test_a_thin_peer_set_produces_no_score_at_all():
    """Four funds cannot support a percentile, so the honest answer is nothing."""
    scored = screener.score_frame(_category_frame(count=peers.MIN_PEERS - 1))
    assert scored["score"].isna().all()
    assert (scored["score_components"] == 0).all()


def test_a_missing_metric_renormalises_instead_of_scoring_zero():
    """A fund too young for a SIP XIRR must not be punished for its age."""
    frame = _category_frame()
    with_sip = screener.score_frame(frame)
    baseline = float(with_sip.loc[with_sip["scheme_code"] == "200007", "score"].iloc[0])

    frame.loc[frame["scheme_code"] == "200007", "sip_xirr_3y_pct"] = np.nan
    without_sip = screener.score_frame(frame)
    row = without_sip.loc[without_sip["scheme_code"] == "200007"].iloc[0]

    assert pd.isna(row["score_consistency"])
    assert row["score_components"] == len(screener.COMPONENT_METRICS) - 1
    # Best-in-class on every remaining component, so the score must not move.
    assert float(row["score"]) == pytest.approx(baseline, abs=0.1)


def test_explain_reconciles_with_the_score_it_explains():
    """Contributions that do not add up to the headline are worse than no table."""
    frame = _category_frame()
    frame.loc[frame["scheme_code"] == "200007", "sip_xirr_3y_pct"] = np.nan
    scored = screener.score_frame(frame)

    breakdown = screener.explain_score(scored, "200007")
    assert breakdown is not None
    assert breakdown.score is not None

    total = sum(
        value * (breakdown.weights[name] / sum(breakdown.weights[n] for n in breakdown.components))
        for name, value in breakdown.components.items()
    )
    assert total == pytest.approx(breakdown.score, abs=0.1)

    text = breakdown.explain()
    assert "renormalised" in text
    assert "consistency" in text  # named as missing, not silently dropped


def test_explain_says_so_when_there_is_no_score():
    scored = screener.score_frame(_category_frame(count=3))
    breakdown = screener.explain_score(scored, "200001")
    assert breakdown is not None
    assert breakdown.score is None
    assert "no score" in breakdown.explain()
    assert str(peers.MIN_PEERS) in breakdown.explain()


def test_explain_returns_none_for_a_scheme_not_in_the_frame():
    assert screener.explain_score(screener.score_frame(_category_frame()), "999999") is None


def test_scoring_an_empty_frame_is_empty():
    assert screener.score_frame(pd.DataFrame()).empty


# ---------------------------------------------------------------------------
# Screening
# ---------------------------------------------------------------------------


def _seed(db_path, frame: pd.DataFrame, monkeypatch) -> None:
    """Point `screen()` at a fixed metric frame, one call per category."""
    by_category = dict(frame.groupby("scheme_category").__iter__())
    monkeypatch.setattr(
        screener.peers,
        "category_metrics",
        lambda _db=None, *, category, **_k: by_category.get(category, pd.DataFrame()),
    )
    monkeypatch.setattr(
        screener.db_manager,
        "search_schemes",
        lambda *_a, **_k: pd.DataFrame({"scheme_category": sorted(by_category)}),
    )


def test_screen_applies_floors_and_ceilings(db_path, monkeypatch):
    _seed(db_path, _category_frame(), monkeypatch)

    result = screener.screen(db_path, minimums={"cagr_3y_pct": 10.0})
    assert not result.empty
    assert result["cagr_3y_pct"].min() >= 10.0

    result = screener.screen(
        db_path, minimums={"cagr_3y_pct": 10.0}, maximums={"volatility_pct": 20.0}
    )
    assert result["volatility_pct"].max() <= 20.0
    assert result["cagr_3y_pct"].min() >= 10.0


def test_screen_filters_by_fund_house_and_name(db_path, monkeypatch):
    _seed(db_path, _category_frame(), monkeypatch)

    assert set(screener.screen(db_path, fund_house="AMC A")["fund_house"]) == {"AMC A"}
    assert list(screener.screen(db_path, query="Fund 3")["scheme_name"]) == ["Fund 3"]


def test_screen_scores_within_each_category_before_combining(db_path, monkeypatch):
    """A liquid fund's 6% must not be scored against an equity fund's 24%."""
    equity = _category_frame(count=8, category="Equity Scheme - Small Cap Fund")
    debt = _category_frame(count=8, category="Debt Scheme - Liquid Fund")
    # Debt returns are an order of magnitude smaller, as they are in reality.
    for column in ("return_1y_pct", "cagr_3y_pct", "cagr_5y_pct", "sip_xirr_3y_pct"):
        debt[column] = debt[column] / 4.0
    debt["volatility_pct"] = debt["volatility_pct"] / 10.0

    _seed(db_path, pd.concat([equity, debt], ignore_index=True), monkeypatch)
    result = screener.screen(db_path, limit=50)

    best_debt = result[result["scheme_category"] == "Debt Scheme - Liquid Fund"]["score"].max()
    best_equity = result[result["scheme_category"] == "Equity Scheme - Small Cap Fund"][
        "score"
    ].max()
    # The best fund in each category scores the same, because each is measured
    # against its own peers. Raw-value scoring would have buried every debt fund.
    assert best_debt == pytest.approx(best_equity, abs=0.1)


def test_screen_returns_empty_when_nothing_is_analysable(db_path, monkeypatch):
    monkeypatch.setattr(screener.db_manager, "search_schemes", lambda *_a, **_k: pd.DataFrame())
    assert screener.screen(db_path).empty


def test_screen_sorts_by_the_requested_column(db_path, monkeypatch):
    _seed(db_path, _category_frame(), monkeypatch)
    result = screener.screen(db_path, sort_by="volatility_pct", ascending=True)
    assert result["volatility_pct"].is_monotonic_increasing


def test_screen_warns_but_still_returns_on_an_unknown_sort_column(db_path, monkeypatch, caplog):
    _seed(db_path, _category_frame(), monkeypatch)
    with caplog.at_level("WARNING"):
        result = screener.screen(db_path, sort_by="not_a_column")
    assert not result.empty
    assert "not_a_column" in caplog.text


def test_summarise_renders_missing_values_as_a_dash_not_a_zero():
    frame = _category_frame()
    frame.loc[frame["scheme_code"] == "200007", "sip_xirr_3y_pct"] = np.nan
    text = screener.summarise(screener.score_frame(frame).sort_values("score", ascending=False))

    assert "—" in text
    assert "not comparable across categories" in text
    assert "not advice" in text


def test_summarise_of_an_empty_screen_says_so():
    assert "No fund matched" in screener.summarise(pd.DataFrame())


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flag", "value"),
    [("min", "cagr_3y_pct=notanumber"), ("min", "made_up_metric=5"), ("max", "volatility_pct")],
)
def test_cli_rejects_a_malformed_bound(flag, value, capsys):
    assert screener.main([f"--{flag}", value]) == 1
    assert "error" in capsys.readouterr().err


def test_cli_prints_a_table_and_writes_a_csv(tmp_path, monkeypatch, capsys):
    _seed(tmp_path / "x.db", _category_frame(), monkeypatch)
    csv = tmp_path / "screen.csv"
    summary = tmp_path / "summary.md"

    assert screener.main(["--csv", str(csv), "--summary-file", str(summary), "--limit", "5"]) == 0

    out = capsys.readouterr().out
    assert "Score" in out and "Risk-adj" in out
    assert csv.exists()
    assert "Fund screen" in summary.read_text(encoding="utf-8")


def test_cli_can_explain_one_scheme(tmp_path, monkeypatch, capsys):
    _seed(tmp_path / "x.db", _category_frame(), monkeypatch)
    assert screener.main(["--explain", "200007"]) == 0
    assert "Category percentile" in capsys.readouterr().out


def test_cli_reports_an_empty_universe_without_failing(tmp_path, monkeypatch, capsys):
    """A new database is a normal state, not an error -- but it must say what to do."""
    monkeypatch.setattr(screener.db_manager, "search_schemes", lambda *_a, **_k: pd.DataFrame())
    assert screener.main([]) == 0
    assert "src.catalogue" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The dashboard tab
# ---------------------------------------------------------------------------


def test_dashboard_screener_tab_renders_and_survives_interaction(tmp_path, monkeypatch):
    """Drives the real app. A tab that exists is not a tab that works -- the blank-
    page and Arrow-serialisation bugs in this dashboard were both invisible to
    unit tests and only appeared when the script actually ran."""
    pytest.importorskip("streamlit")
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    from src import catalogue, config, db_manager, fetch_amfi_data

    db = tmp_path / "app.db"
    conn = db_manager.setup_database(db)
    # Enough funds in one category to clear MIN_PEERS, or the tab shows the
    # too-few-peers notice instead of a screen and asserts nothing useful.
    codes = [str(119590 + i) for i in range(10)]
    fetch_amfi_data.generate_synthetic_history(conn, codes, years=4.0)
    db_manager.upsert_catalogue(
        conn,
        [
            catalogue.CatalogueEntry(
                scheme_code=code,
                scheme_name=f"Test Fund {i}",
                fund_house=f"AMC {i % 3}",
                scheme_type="Open Ended Schemes",
                scheme_category="Equity Scheme - Large Cap Fund",
            )
            for i, code in enumerate(codes)
        ],
    )
    conn.close()

    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(config, "AUTO_BOOTSTRAP", False)

    entry = Path(__file__).resolve().parent.parent / "streamlit_app.py"
    app = AppTest.from_file(str(entry), default_timeout=300)
    app.run()
    assert not app.exception

    tab = next(t for t in app.tabs if t.label == "Screener")
    assert tab.dataframe, "the screen rendered no table"
    columns = list(tab.dataframe[0].value.columns)
    # The score never travels without its components.
    assert "score" in columns
    for component in screener.COMPONENT_METRICS:
        assert f"score_{component}" in columns

    breakdown = next(
        (str(block.value) for block in tab.markdown if "Category percentile" in str(block.value)),
        None,
    )
    assert breakdown is not None, "the score was shown without its breakdown"
    assert "**Score**" in breakdown

    # Filtering must not blank the page -- the failure mode that shipped before.
    tab.number_input[0].set_value(5.0).run()
    assert not app.exception
    assert any(t.label == "Screener" for t in app.tabs)


def test_dashboard_detail_tab_ranks_a_fund_against_its_category(tmp_path, monkeypatch):
    """The percentile table must be direction-corrected in the UI, not just the core."""
    pytest.importorskip("streamlit")
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    from src import catalogue, config, db_manager, fetch_amfi_data

    db = tmp_path / "app.db"
    conn = db_manager.setup_database(db)
    codes = [str(119590 + i) for i in range(10)]
    fetch_amfi_data.generate_synthetic_history(conn, codes, years=4.0)
    db_manager.upsert_catalogue(
        conn,
        [
            catalogue.CatalogueEntry(
                scheme_code=code,
                scheme_name=f"Test Fund {i}",
                fund_house="AMC A",
                scheme_type="Open Ended Schemes",
                scheme_category="Equity Scheme - Large Cap Fund",
            )
            for i, code in enumerate(codes)
        ],
    )
    conn.close()

    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(config, "AUTO_BOOTSTRAP", False)

    app = AppTest.from_file(
        str(Path(__file__).resolve().parent.parent / "streamlit_app.py"), default_timeout=300
    )
    app.run()
    assert not app.exception

    detail = next(t for t in app.tabs if t.label == "Scheme detail")
    ranks = next((d.value for d in detail.dataframe if "Percentile" in list(d.value.columns)), None)
    assert ranks is not None, "no peer ranking rendered in the detail tab"
    assert {"Metric", "This fund", "Category median", "Percentile", "Quartile"} <= set(
        ranks.columns
    )
    assert ranks["Percentile"].between(0, 100).all()

    # The fund with below-median volatility must rank *above* the median, not below.
    volatility = ranks.loc[ranks["Metric"] == "volatility_pct"]
    if not volatility.empty:
        row = volatility.iloc[0]
        if row["This fund"] < row["Category median"]:
            assert row["Percentile"] > 50, "low volatility was scored as bad"
