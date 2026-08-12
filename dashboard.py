"""
Streamlit dashboard over the analysis layer.

The dashboard deliberately calls `analyzer.analyse()` rather than recomputing
returns inline. The previous version duplicated the 1Y/3Y maths in the UI, which
meant the dashboard and the report could disagree about the same fund. One
analysis engine, several presentations.

    streamlit run dashboard.py

On a hosted deployment the filesystem is ephemeral, so an empty database is the
normal first-load state rather than an error; see `src/bootstrap.py`.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src import analyzer, bootstrap, config, db_manager, metrics, peers, screener

try:
    import plotly.express as px
    import plotly.graph_objects as go

    PLOTLY = True
except ImportError:  # pragma: no cover - optional dependency
    PLOTLY = False

st.set_page_config(
    page_title="Indian Mutual Fund Tracker & Analytics",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# Data access (cached; TTL keeps a long-lived session from going stale)
# ---------------------------------------------------------------------------


@st.cache_data(ttl=config.CACHE_TTL_SECONDS, show_spinner="Loading fund catalogue…")
def load_catalogue() -> pd.DataFrame:
    return db_manager.scheme_catalogue()


@st.cache_data(ttl=config.CACHE_TTL_SECONDS, show_spinner="Computing performance and risk metrics…")
def run_analysis(codes: tuple[str, ...], benchmark: str | None, risk_free: float):
    """Cached analysis. Returns the flat metric frame plus objects the UI needs.

    The `AnalysisResult` holds pandas objects, so the cache key is the argument
    tuple; Streamlit hashes only the primitives passed in here.
    """
    return analyzer.analyse(
        scheme_codes=list(codes), benchmark_code=benchmark, risk_free_rate=risk_free
    )


@st.cache_data(ttl=config.CACHE_TTL_SECONDS)
def load_runs() -> pd.DataFrame:
    return db_manager.recent_runs(limit=5)


@st.cache_data(ttl=config.CACHE_TTL_SECONDS)
def catalogue_coverage() -> dict[str, int]:
    """How much of the AMFI universe is catalogued, and how much is analysable."""
    return db_manager.catalogue_stats()


@st.cache_data(ttl=config.CACHE_TTL_SECONDS, show_spinner="Scoring the category…")
def score_category(category: str, risk_free: float) -> pd.DataFrame:
    """Percentile-scored metrics for one AMFI category.

    Cached per category because it recomputes every metric for every fund in it,
    which is far too slow to redo on each widget interaction.
    """
    return screener.score_category(category=category, risk_free_rate=risk_free)


@st.cache_data(ttl=config.CACHE_TTL_SECONDS)
def analysable_categories() -> list[str]:
    """Categories with at least one fund holding enough history to analyse."""
    universe = db_manager.search_schemes(with_history_only=True, limit=10_000)
    if universe.empty:
        return []
    return sorted(str(name) for name in universe["scheme_category"].dropna().unique())


# `cache_resource` rather than `cache_data`: this is a side effect that must run
# at most once per container, not a value to memoise per session. On Streamlit
# Community Cloud the filesystem is wiped on every restart, so this is what keeps
# a hosted deployment from greeting visitors with an empty database.
@st.cache_resource(show_spinner="Fetching NAV history from AMFI (first load only)…")
def bootstrap_data() -> bootstrap.BootstrapResult:
    config.ensure_directories()
    return bootstrap.ensure_database(enabled=config.AUTO_BOOTSTRAP)


boot = bootstrap_data()
catalogue = load_catalogue()

if catalogue.empty:
    st.title("📈 Indian Mutual Fund Tracker")
    if boot.status == "failed":
        st.error(f"**Could not load NAV data.** {boot.message}", icon="🚨")
        if st.button("Try again"):
            st.cache_resource.clear()
            st.cache_data.clear()
            st.rerun()
    else:
        st.warning(
            "The database is empty. Populate it with:\n\n"
            "```bash\npython main_pipeline.py\n```\n\n"
            "For offline development without AMFI access, use `--synthetic-only` "
            "(the data will be clearly labelled as generated)."
        )
        if boot.status == "skipped":
            st.caption(boot.message)
    st.stop()

# ---------------------------------------------------------------------------
# Sidebar controls
# ---------------------------------------------------------------------------

st.sidebar.title("🔍 Analysis controls")

houses = sorted(catalogue["fund_house"].dropna().astype(str).unique())
selected_houses = st.sidebar.multiselect("Fund house (AMC)", houses, default=houses)

available = catalogue[catalogue["fund_house"].astype(str).isin(selected_houses)]
if available.empty:
    available = catalogue

name_to_code = dict(
    zip(available["scheme_name"], available["scheme_code"].astype(str), strict=True)
)
selected_names = st.sidebar.multiselect(
    "Schemes to analyse",
    list(name_to_code),
    default=list(name_to_code)[: min(3, len(name_to_code))],
)
selected_codes = tuple(name_to_code[name] for name in selected_names)

benchmark_options = {"None (skip relative metrics)": None} | {
    f"{row.scheme_name}": str(row.scheme_code) for row in catalogue.itertuples()
}
default_benchmark_label = next(
    (label for label, code in benchmark_options.items() if code == str(config.BENCHMARK_SCHEME)),
    "None (skip relative metrics)",
)
benchmark_label = st.sidebar.selectbox(
    "Benchmark",
    list(benchmark_options),
    index=list(benchmark_options).index(default_benchmark_label),
    help="Used for alpha, beta, tracking error, information ratio, and capture ratios.",
)
benchmark_code = benchmark_options[benchmark_label]

risk_free = (
    st.sidebar.number_input(
        "Risk-free rate (% annual)",
        min_value=0.0,
        max_value=20.0,
        value=config.RISK_FREE_RATE * 100,
        step=0.25,
        help="Drives Sharpe, Sortino, and alpha. Roughly the 10-year G-Sec yield.",
    )
    / 100.0
)

col_reload, col_fetch = st.sidebar.columns(2)
if col_reload.button("↻ Recompute", width="stretch", help="Re-read the database"):
    st.cache_data.clear()
    st.rerun()
if col_fetch.button("⤓ Fetch NAVs", width="stretch", help="Pull fresh NAVs from AMFI"):
    with st.spinner("Fetching from AMFI…"):
        refreshed = bootstrap.ensure_database(force=True)
    st.cache_data.clear()
    st.cache_resource.clear()
    (st.sidebar.success if refreshed.ok else st.sidebar.error)(refreshed.message)
    st.rerun()

st.sidebar.divider()
if boot.status == "fetched":
    st.sidebar.caption(f"Bootstrapped this session: {boot.message}")
runs = load_runs()
if not runs.empty:
    latest = runs.iloc[0]
    st.sidebar.caption(
        f"Last ingestion: {latest['started_at']} · status **{latest['status']}** · "
        f"{latest['rows_written']} new rows"
    )

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.title("📈 Mutual Fund Performance & Risk Dashboard")

if not selected_codes:
    st.info("Select at least one scheme in the sidebar.")
    st.stop()

result = run_analysis(selected_codes, benchmark_code, risk_free)

if result.has_synthetic_data:
    st.error(
        "**This view contains synthetic NAV data.** The live AMFI fetch was unavailable when "
        "these rows were written, so the figures are generated for pipeline testing and are "
        "not real returns.",
        icon="🚨",
    )

if not result.schemes:
    st.warning(
        "No selected scheme passed the data-quality gate. See the Data quality tab for details."
    )

st.caption(
    f"NAV as of **{result.as_of or 'n/a'}** · {len(result.schemes)} scheme(s) analysed"
    + (f" · benchmark: **{result.benchmark_name}**" if result.benchmark_name else "")
)

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

overview_tab, risk_tab, chart_tab, screen_tab, quality_tab, detail_tab = st.tabs(
    ["Overview", "Risk", "Charts", "Screener", "Data quality", "Scheme detail"]
)


def styled(frame: pd.DataFrame) -> pd.DataFrame:
    """Drop all-empty columns so the tables stay readable."""
    return frame.dropna(axis=1, how="all")


with overview_tab:
    if result.insights:
        st.subheader("What the numbers say")
        for insight in result.insights[:6]:
            with st.container(border=True):
                st.markdown(f"**{insight.headline}**")
                st.caption(insight.detail)

    frame = result.to_frame()
    if not frame.empty:
        st.subheader("Performance")
        columns = [
            "scheme_name",
            "latest_nav",
            "return_3m_pct",
            "return_6m_pct",
            "return_1y_pct",
            "cagr_3y_pct",
            "cagr_5y_pct",
            "sip_xirr_3y_pct",
            "cagr_since_inception_pct",
        ]
        st.dataframe(
            styled(frame[[c for c in columns if c in frame]]),
            width="stretch",
            hide_index=True,
            column_config={
                "scheme_name": st.column_config.TextColumn("Scheme", width="large"),
                "latest_nav": st.column_config.NumberColumn("NAV (₹)", format="%.2f"),
                "return_3m_pct": st.column_config.NumberColumn("3M", format="%+.2f%%"),
                "return_6m_pct": st.column_config.NumberColumn("6M", format="%+.2f%%"),
                "return_1y_pct": st.column_config.NumberColumn("1Y", format="%+.2f%%"),
                "cagr_3y_pct": st.column_config.NumberColumn("3Y CAGR", format="%+.2f%%"),
                "cagr_5y_pct": st.column_config.NumberColumn("5Y CAGR", format="%+.2f%%"),
                "sip_xirr_3y_pct": st.column_config.NumberColumn("SIP XIRR 3Y", format="%+.2f%%"),
                "cagr_since_inception_pct": st.column_config.NumberColumn(
                    "Since inception", format="%+.2f%%"
                ),
            },
        )
        st.caption(
            "Returns under one year are absolute; one year and beyond are annualised. "
            "SIP XIRR reflects a monthly investment schedule rather than a lump sum."
        )

with risk_tab:
    frame = result.to_frame()
    if frame.empty:
        st.info("No metrics available.")
    else:
        st.subheader("Risk & risk-adjusted return")
        columns = [
            "scheme_name",
            "volatility_pct",
            "downside_deviation_pct",
            "sharpe_ratio",
            "sortino_ratio",
            "calmar_ratio",
            "max_drawdown_pct",
            "max_drawdown_recovered",
            "current_drawdown_pct",
            "var_95_pct",
            "cvar_95_pct",
            "positive_days_pct",
        ]
        st.dataframe(
            styled(frame[[c for c in columns if c in frame]]),
            width="stretch",
            hide_index=True,
        )
        st.caption(
            f"Sharpe and Sortino use a {risk_free * 100:.2f}% annual risk-free rate. "
            "VaR/CVaR are one-day historical estimates at 95% confidence."
        )

        benched = [s for s in result.schemes if s.benchmark]
        if benched:
            st.subheader(f"Versus {result.benchmark_name}")
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Scheme": s.scheme_name,
                            "Alpha (ann. %)": s.benchmark.alpha_pct,
                            "Beta": s.benchmark.beta,
                            "R²": s.benchmark.r_squared,
                            "Tracking error (%)": s.benchmark.tracking_error_pct,
                            "Information ratio": s.benchmark.information_ratio,
                            "Up capture (%)": s.benchmark.up_capture_pct,
                            "Down capture (%)": s.benchmark.down_capture_pct,
                            "1Y excess (%)": s.benchmark.excess_return_1y_pct,
                        }
                        for s in benched
                    ]
                ),
                width="stretch",
                hide_index=True,
            )
            st.caption(
                "Down capture below 100% means the fund fell less than the benchmark in down "
                "months. R² above 0.95 suggests the fund is behaving like an index tracker."
            )

        rolling = [s for s in result.schemes if s.rolling_3y]
        if rolling:
            st.subheader("Rolling 3-year return consistency")
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Scheme": s.scheme_name,
                            "Windows": s.rolling_3y.observations,
                            "Worst (%)": s.rolling_3y.min_pct,
                            "Median (%)": s.rolling_3y.median_pct,
                            "Best (%)": s.rolling_3y.max_pct,
                            "% positive": s.rolling_3y.positive_share_pct,
                            "% above risk-free": s.rolling_3y.beat_hurdle_share_pct,
                        }
                        for s in rolling
                    ]
                ),
                width="stretch",
                hide_index=True,
            )
            st.caption(
                "Every overlapping 3-year window, annualised. A wide spread means the headline "
                "return depends heavily on the purchase date."
            )

with chart_tab:
    codes = [s.scheme_code for s in result.schemes]
    labels = {s.scheme_code: s.scheme_name for s in result.schemes}
    if result.benchmark_code and result.benchmark_code in result.nav_series:
        codes = [*codes, result.benchmark_code]
        labels[result.benchmark_code] = f"{result.benchmark_name} (benchmark)"
    series_map = {labels[c]: result.nav_series[c] for c in codes if c in result.nav_series}

    if not series_map:
        st.info("No NAV series to plot.")
    else:
        common_start = max(s.index[0] for s in series_map.values())
        st.subheader(f"Growth of ₹{config.GROWTH_CHART_INITIAL:,.0f}")
        st.caption(f"All series rebased to a common start date of {common_start:%d %b %Y}.")
        growth = pd.DataFrame(
            {
                label: metrics.growth_of(series.loc[common_start:], config.GROWTH_CHART_INITIAL)
                for label, series in series_map.items()
            }
        )
        drawdowns = pd.DataFrame(
            {label: metrics.drawdown_series(series) for label, series in series_map.items()}
        )

        if PLOTLY:
            figure = px.line(growth, labels={"value": "Value (₹)", "index": "Date"})
            figure.update_layout(
                hovermode="x unified",
                legend_title_text="",
                margin={"l": 0, "r": 0, "t": 10, "b": 0},
            )
            figure.update_xaxes(
                rangeslider_visible=True,
                rangeselector={
                    "buttons": [
                        {"count": 1, "label": "1m", "step": "month", "stepmode": "backward"},
                        {"count": 6, "label": "6m", "step": "month", "stepmode": "backward"},
                        {"count": 1, "label": "1y", "step": "year", "stepmode": "backward"},
                        {"count": 3, "label": "3y", "step": "year", "stepmode": "backward"},
                        {"count": 5, "label": "5y", "step": "year", "stepmode": "backward"},
                        {"step": "all"},
                    ]
                },
            )
            st.plotly_chart(figure, width="stretch")

            st.subheader("Drawdown from running peak")
            dd_figure = go.Figure()
            for column in drawdowns:
                dd_figure.add_trace(
                    go.Scatter(x=drawdowns.index, y=drawdowns[column], name=column, fill="tozeroy")
                )
            dd_figure.update_layout(
                hovermode="x unified",
                yaxis_title="Drawdown (%)",
                margin={"l": 0, "r": 0, "t": 10, "b": 0},
            )
            st.plotly_chart(dd_figure, width="stretch")
        else:
            st.line_chart(growth)
            st.subheader("Drawdown from running peak")
            st.area_chart(drawdowns)
        st.caption(
            "Drawdown answers the question CAGR hides: how far down did this fund go, and how "
            "long did it stay there?"
        )

with screen_tab:
    st.subheader("Screen the universe")
    coverage = catalogue_coverage()
    categories = analysable_categories()

    st.caption(
        f"{coverage['schemes']:,} scheme(s) catalogued across {coverage['categories']} "
        f"categories; {coverage['analysable']:,} have enough NAV history to analyse. "
        "The scheduled catalogue job backfills more history every day."
    )

    if not categories:
        st.info(
            "No category has enough history to screen yet. Run "
            "`python -m src.catalogue --backfill 100` to fetch the full scheme "
            "universe and start filling in history."
        )
    else:
        chosen = st.selectbox("Category", categories, key="screen_category")
        scored = score_category(chosen, risk_free)

        if scored.empty:
            st.info(f"No analysable fund in {chosen} yet.")
        elif len(scored) < peers.MIN_PEERS:
            # Deliberately no score: a percentile against four funds is noise
            # dressed as precision, and the table would look authoritative anyway.
            st.warning(
                f"Only {len(scored)} analysable fund(s) in this category. At least "
                f"{peers.MIN_PEERS} are needed before a percentile means anything, so "
                "no score is shown.",
                icon="⚠️",
            )
            st.dataframe(styled(scored), width="stretch", hide_index=True)
        else:
            controls = st.columns(3)
            min_cagr = controls[0].number_input(
                "Minimum 3Y CAGR %", value=0.0, step=1.0, key="screen_min_cagr"
            )
            max_vol = controls[1].number_input(
                "Maximum volatility %", value=100.0, step=1.0, key="screen_max_vol"
            )
            house = controls[2].text_input("Fund house contains", key="screen_house")

            view = scored.copy()
            view = view[
                pd.to_numeric(view["cagr_3y_pct"], errors="coerce").fillna(-1e9) >= min_cagr
            ]
            view = view[
                pd.to_numeric(view["volatility_pct"], errors="coerce").fillna(1e9) <= max_vol
            ]
            if house:
                view = view[
                    view["fund_house"].astype(str).str.contains(house, case=False, na=False)
                ]

            st.caption(
                f"{len(view)} of {len(scored)} fund(s) match. Scores are **category "
                "percentiles** (0–100), weighted "
                + ", ".join(f"{n} {w:.0%}" for n, w in screener.DEFAULT_WEIGHTS.items())
                + ". A score ranks a fund against its own category, never across "
                "categories — and it is not advice."
            )

            columns = [
                "scheme_name",
                "fund_house",
                "score",
                "score_returns",
                "score_risk_adjusted",
                "score_drawdown",
                "score_consistency",
                "cagr_3y_pct",
                "volatility_pct",
                "sharpe_ratio",
                "max_drawdown_pct",
            ]
            st.dataframe(
                styled(view[[c for c in columns if c in view.columns]]),
                width="stretch",
                hide_index=True,
            )

            if not view.empty:
                st.subheader("Why a fund scores what it does")
                pick = st.selectbox("Fund", list(view["scheme_name"]), key="screen_explain_scheme")
                code = view.loc[view["scheme_name"] == pick, "scheme_code"].iloc[0]
                breakdown = screener.explain_score(scored, str(code))
                if breakdown is not None:
                    # The components travel with the score, always: 68 from strong
                    # returns and a deep drawdown is a different fund from 68 across
                    # the board, and a single number cannot tell them apart.
                    st.markdown(breakdown.explain())

            st.download_button(
                "Download this screen (CSV)",
                view.to_csv(index=False).encode(),
                file_name=f"screen_{chosen.replace(' ', '_')}.csv",
                mime="text/csv",
            )


with quality_tab:
    summary = result.quality.summary()
    columns = st.columns(5)
    for column, (label, value) in zip(
        columns,
        [
            ("Schemes checked", summary["schemes_checked"]),
            ("Critical", summary["critical"]),
            ("Warnings", summary["warnings"]),
            ("Informational", summary["info"]),
            ("Excluded", summary["schemes_excluded"]),
        ],
        strict=True,
    ):
        column.metric(label, value)

    findings = result.quality.sorted_findings()
    if findings:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Severity": f.severity,
                        "Scheme": f.scheme_code,
                        "Check": f.check,
                        "Detail": f.message,
                    }
                    for f in findings
                ]
            ),
            width="stretch",
            hide_index=True,
        )
    else:
        st.success("All data-quality checks passed.")

    st.subheader("Coverage")
    if result.quality.coverage:
        st.dataframe(
            pd.DataFrame(result.quality.coverage).T.reset_index(names="scheme_code"),
            width="stretch",
            hide_index=True,
        )

    st.subheader("Assumptions")
    st.dataframe(
        # Values are deliberately mixed types (rates, day counts, prose), and Arrow
        # cannot serialise a mixed column -- render everything as text.
        pd.DataFrame(
            [(k, str(v)) for k, v in result.assumptions.items()],
            columns=["Assumption", "Value"],
        ),
        width="stretch",
        hide_index=True,
    )

    if not runs.empty:
        st.subheader("Ingestion history")
        st.dataframe(runs, width="stretch", hide_index=True)

with detail_tab:
    if not result.schemes:
        st.info("No scheme passed the quality gate.")
    else:
        choice = st.selectbox(
            "Scheme", [s.scheme_name for s in result.schemes], key="detail_scheme"
        )
        scheme = next(s for s in result.schemes if s.scheme_name == choice)

        top = st.columns(5)
        top[0].metric("Current NAV", f"₹{scheme.latest_nav:,.2f}")
        top[1].metric("As of", scheme.as_of.strftime("%d %b %Y"))
        top[2].metric(
            "1Y return",
            f"{scheme.return_1y_pct:+.2f}%" if scheme.return_1y_pct is not None else "—",
        )
        top[3].metric(
            "3Y CAGR", f"{scheme.cagr_3y_pct:+.2f}%" if scheme.cagr_3y_pct is not None else "—"
        )
        top[4].metric(
            "Sharpe", f"{scheme.sharpe_ratio:.2f}" if scheme.sharpe_ratio is not None else "—"
        )

        second = st.columns(5)
        second[0].metric(
            "Volatility",
            f"{scheme.volatility_pct:.2f}%" if scheme.volatility_pct is not None else "—",
        )
        second[1].metric(
            "Max drawdown",
            f"{scheme.max_drawdown_pct:.1f}%" if scheme.max_drawdown_pct is not None else "—",
            help=(
                f"Peak {scheme.max_drawdown_peak} → trough {scheme.max_drawdown_trough}; "
                + ("recovered" if scheme.max_drawdown_recovered else "not yet recovered")
            ),
        )
        second[2].metric(
            "Current drawdown",
            f"{scheme.current_drawdown_pct:.1f}%"
            if scheme.current_drawdown_pct is not None
            else "—",
        )
        second[3].metric(
            "SIP XIRR (3Y)",
            f"{scheme.sip_xirr_3y_pct:+.2f}%" if scheme.sip_xirr_3y_pct is not None else "—",
        )
        second[4].metric("Trend", scheme.sma_signal)

        if scheme.notes:
            for note in scheme.notes:
                st.warning(note)

        # "Where does it sit among its peers?" is usually the more useful question
        # than "did it beat the index?", and it needs the category catalogue.
        if scheme.scheme_category:
            st.subheader(f"Against its category: {scheme.scheme_category}")
            category_frame = score_category(scheme.scheme_category, risk_free)
            comparison = (
                peers.compare_within_category(
                    category_frame, scheme.scheme_code, scheme.scheme_category
                )
                if not category_frame.empty
                else None
            )
            if comparison is None:
                st.caption(
                    f"Fewer than {peers.MIN_PEERS} funds in this category have enough "
                    "stored history to rank against, so no percentile is shown. The "
                    "scheduled catalogue job fills in more history each day."
                )
            else:
                ranks = pd.DataFrame(
                    [
                        {
                            "Metric": rank.metric,
                            "This fund": round(rank.value, 2),
                            "Category median": round(rank.category_median, 2),
                            "Percentile": round(rank.percentile),
                            "Quartile": f"Q{rank.quartile}",
                        }
                        for rank in comparison.ranks.values()
                    ]
                )
                st.dataframe(ranks, width="stretch", hide_index=True)
                st.caption(
                    f"Ranked against {comparison.peers} peer(s). Percentiles are "
                    "direction-corrected, so 100 is always good — including for "
                    "volatility and drawdown, where a lower raw value is better."
                )
                strengths = comparison.top_quartile_metrics()
                weaknesses = comparison.bottom_quartile_metrics()
                if strengths:
                    st.success("Top quartile: " + ", ".join(strengths), icon="✅")
                if weaknesses:
                    st.warning("Bottom quartile: " + ", ".join(weaknesses), icon="⚠️")

        st.subheader("NAV with moving averages")
        nav = result.nav_series[scheme.scheme_code]
        chart = pd.DataFrame(
            {
                "NAV": nav,
                "50D SMA": nav.rolling(50, min_periods=50).mean(),
                "200D SMA": nav.rolling(200, min_periods=200).mean(),
            }
        )
        st.line_chart(chart)
        st.caption(
            "Moving averages start only once the full window is available — a 50-day average "
            "computed from 12 observations would not be a 50-day average."
        )

        with st.expander("Raw NAV history"):
            st.dataframe(
                nav.sort_index(ascending=False).rename("NAV").reset_index(),
                width="stretch",
                hide_index=True,
            )
            st.download_button(
                "Download NAV history (CSV)",
                nav.rename("nav").to_csv().encode(),
                file_name=f"nav_{scheme.scheme_code}.csv",
                mime="text/csv",
            )

st.divider()
st.caption(config.DISCLAIMER)
