"""
Report rendering: Markdown, JSON, and a self-contained HTML page.

Three outputs, one analysis:

* **Markdown** -- written to ``$GITHUB_STEP_SUMMARY`` so the analysis is visible
  on the Actions run page itself, with no artifact download.
* **JSON** -- the machine-readable contract for anything downstream.
* **HTML** -- a single file with inline SVG charts and no external requests,
  published to GitHub Pages. Charts are hand-rendered SVG rather than a plotting
  library so the page works offline, needs no CDN, and survives a strict CSP.

Every report leads with data provenance and closes with assumptions and a
disclaimer, because a performance number without its assumptions is not a
finding -- it is a claim.
"""

from __future__ import annotations

import html
import json
import logging
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from src import config, metrics
from src.analyzer import AnalysisResult

logger = logging.getLogger(__name__)

SERIES_COLOURS = ["#2563eb", "#db2777", "#059669", "#d97706", "#7c3aed", "#0891b2"]


def _n(value: object, digits: int = 2, suffix: str = "") -> str:
    """Format a possibly-missing number. Missing renders as an em dash, never 0."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    if isinstance(value, (int, float)):
        return f"{value:,.{digits}f}{suffix}"
    return str(value)


def _signed(value: float | None, digits: int = 2, suffix: str = "%") -> str:
    if value is None:
        return "—"
    return f"{value:+,.{digits}f}{suffix}"


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def render_markdown(result: AnalysisResult) -> str:
    """Full analysis as Markdown, suitable for a GitHub Actions job summary."""
    lines: list[str] = []
    add = lines.append

    add(f"# {config.REPORT_TITLE}")
    add("")
    add(
        f"**Generated:** {result.generated_at:%Y-%m-%d %H:%M} &nbsp;|&nbsp; "
        f"**NAV as of:** {result.as_of or 'n/a'} &nbsp;|&nbsp; "
        f"**Schemes analysed:** {len(result.schemes)}"
    )
    add("")

    if result.has_synthetic_data:
        add("> [!CAUTION]")
        add(
            "> **This report contains SYNTHETIC NAV data.** The live AMFI fetch was "
            "unavailable, so figures for the affected schemes are generated, not real. "
            "Do not use them for any investment purpose."
        )
        add("")
    if result.quality.has_critical:
        add("> [!WARNING]")
        add(
            f"> {len(result.quality.critical)} critical data-quality finding(s); "
            f"{len(result.quality.unusable_schemes())} scheme(s) excluded from analysis."
        )
        add("")

    # --- Executive summary ---
    add("## Executive summary")
    add("")
    if not result.insights:
        add("_No findings: the analysis produced no scheme-level results._")
    for insight in result.insights[:8]:
        add(f"{insight.rank}. **{insight.headline}** — {insight.detail}")
    add("")

    if not result.schemes:
        add("")
        add(_quality_section(result))
        return "\n".join(lines)

    # --- Performance ---
    add("## Performance")
    add("")
    add("| Scheme | NAV (₹) | 3M | 6M | 1Y | 3Y CAGR | 5Y CAGR | SIP XIRR (3Y) | Since inception |")
    add("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for scheme in result.ranked_by("cagr_3y_pct"):
        add(
            f"| {scheme.scheme_name} | {_n(scheme.latest_nav)} | "
            f"{_signed(scheme.return_3m_pct)} | {_signed(scheme.return_6m_pct)} | "
            f"{_signed(scheme.return_1y_pct)} | {_signed(scheme.cagr_3y_pct)} | "
            f"{_signed(scheme.cagr_5y_pct)} | {_signed(scheme.sip_xirr_3y_pct)} | "
            f"{_signed(scheme.cagr_since_inception_pct)} |"
        )
    for scheme in result.schemes:
        if scheme.cagr_3y_pct is None:
            add(
                f"| {scheme.scheme_name} | {_n(scheme.latest_nav)} | "
                f"{_signed(scheme.return_3m_pct)} | {_signed(scheme.return_6m_pct)} | "
                f"{_signed(scheme.return_1y_pct)} | — | — | — | "
                f"{_signed(scheme.cagr_since_inception_pct)} |"
            )
    add("")
    add(
        "_Returns under one year are absolute; one year and beyond are annualised (CAGR). "
        "SIP XIRR models a monthly investment, which weights recent contributions far more "
        "heavily than lump-sum CAGR does._"
    )
    add("")

    # --- Risk ---
    add("## Risk & risk-adjusted return")
    add("")
    add(
        "| Scheme | Volatility | Downside dev. | Sharpe | Sortino | Calmar | Max drawdown | "
        "Recovered | Current DD | VaR 95% | CVaR 95% |"
    )
    add("|---|---:|---:|---:|---:|---:|---:|:--:|---:|---:|---:|")
    for scheme in result.ranked_by("sharpe_ratio") or result.schemes:
        recovered = (
            "—"
            if scheme.max_drawdown_recovered is None
            else ("yes" if scheme.max_drawdown_recovered else "**no**")
        )
        add(
            f"| {scheme.scheme_name} | {_n(scheme.volatility_pct, 2, '%')} | "
            f"{_n(scheme.downside_deviation_pct, 2, '%')} | {_n(scheme.sharpe_ratio)} | "
            f"{_n(scheme.sortino_ratio)} | {_n(scheme.calmar_ratio)} | "
            f"{_n(scheme.max_drawdown_pct, 1, '%')} | {recovered} | "
            f"{_n(scheme.current_drawdown_pct, 1, '%')} | {_n(scheme.var_95_pct, 2, '%')} | "
            f"{_n(scheme.cvar_95_pct, 2, '%')} |"
        )
    add("")
    add(
        f"_Sharpe and Sortino use a {result.assumptions['risk_free_rate_annual_pct']}% annual "
        "risk-free rate. VaR/CVaR are one-day historical estimates: on the worst 5% of days the "
        "fund lost at least the VaR figure, and CVaR is the average loss on those days._"
    )
    add("")

    # --- Benchmark ---
    benched = [s for s in result.schemes if s.benchmark]
    if benched:
        add(f"## Versus benchmark — {result.benchmark_name}")
        add("")
        add(
            "| Scheme | Alpha (ann.) | Beta | R² | Tracking error | Information ratio | "
            "Up capture | Down capture | 1Y excess |"
        )
        add("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for scheme in benched:
            b = scheme.benchmark
            add(
                f"| {scheme.scheme_name} | {_signed(b.alpha_pct)} | {_n(b.beta)} | "
                f"{_n(b.r_squared, 3)} | {_n(b.tracking_error_pct, 2, '%')} | "
                f"{_n(b.information_ratio)} | {_n(b.up_capture_pct, 1, '%')} | "
                f"{_n(b.down_capture_pct, 1, '%')} | {_signed(b.excess_return_1y_pct)} |"
            )
        add("")
        add(
            "_Alpha and beta come from a CAPM regression of daily excess returns. Down capture "
            "below 100% means the fund fell less than the benchmark in down months — the single "
            "most useful column here for a risk-averse investor. A negative capture figure means "
            "the fund moved opposite to the benchmark in those months, which usually signals a "
            "low correlation rather than skill._"
        )
        add("")

    # --- Rolling consistency ---
    rolling = [s for s in result.schemes if s.rolling_3y]
    if rolling:
        add("## Rolling 3-year return consistency")
        add("")
        add("| Scheme | Windows | Worst | Median | Best | % positive | % above risk-free |")
        add("|---|---:|---:|---:|---:|---:|---:|")
        for scheme in rolling:
            r = scheme.rolling_3y
            add(
                f"| {scheme.scheme_name} | {r.observations:,} | {_signed(r.min_pct)} | "
                f"{_signed(r.median_pct)} | {_signed(r.max_pct)} | "
                f"{_n(r.positive_share_pct, 1, '%')} | {_n(r.beat_hurdle_share_pct, 1, '%')} |"
            )
        add("")
        add(
            "_Every overlapping 3-year window in the history, annualised. A high median with a "
            "deeply negative worst case means the fund's headline number depends on when you "
            "bought._"
        )
        add("")

    # --- Trend ---
    add("## Trend (moving averages)")
    add("")
    add("| Scheme | NAV | 50D SMA | 200D SMA | NAV vs 50D | Signal |")
    add("|---|---:|---:|---:|---:|:--|")
    for scheme in result.schemes:
        add(
            f"| {scheme.scheme_name} | {_n(scheme.latest_nav)} | {_n(scheme.sma_50)} | "
            f"{_n(scheme.sma_200)} | {_signed(scheme.nav_vs_sma50_pct)} | {scheme.sma_signal} |"
        )
    add("")

    add(_quality_section(result))
    add("")
    add(_assumptions_section(result))
    return "\n".join(lines)


def _quality_section(result: AnalysisResult) -> str:
    lines = ["## Data quality", ""]
    summary = result.quality.summary()
    lines.append(
        f"{summary['schemes_checked']} scheme(s) checked · "
        f"**{summary['critical']} critical** · {summary['warnings']} warning(s) · "
        f"{summary['info']} informational · {summary['schemes_excluded']} excluded"
    )
    lines.append("")
    findings = result.quality.sorted_findings()
    if not findings:
        lines.append("All checks passed.")
        return "\n".join(lines)
    lines.append("| Severity | Scheme | Check | Detail |")
    lines.append("|:--|:--|:--|:--|")
    for finding in findings:
        lines.append(
            f"| {finding.severity} | {finding.scheme_code} | `{finding.check}` | {finding.message} |"
        )
    lines.append("")
    lines.append("### Coverage")
    lines.append("")
    lines.append("| Scheme | Observations | First | Last | Staleness | History | Source |")
    lines.append("|:--|---:|:--|:--|---:|---:|:--|")
    for code, cover in sorted(result.quality.coverage.items()):
        lines.append(
            f"| {code} | {cover['observations']:,} | {cover['first_date']} | "
            f"{cover['last_date']} | {cover['staleness_days']}d | "
            f"{cover['history_years']}y | {cover['data_source']} |"
        )
    return "\n".join(lines)


def _assumptions_section(result: AnalysisResult) -> str:
    lines = ["## Methodology & assumptions", "", "| Assumption | Value |", "|:--|:--|"]
    for key, value in result.assumptions.items():
        lines.append(f"| {key.replace('_', ' ').capitalize()} | {value} |")
    lines += [
        "",
        "Metric definitions: CAGR compounds over calendar time (365.25-day years); volatility, "
        "Sharpe, Sortino and tracking error annualise daily statistics by √252; max drawdown is "
        "the worst peak-to-trough NAV decline; Calmar is CAGR ÷ |max drawdown|; XIRR solves the "
        "cashflow IRR on a 365-day basis.",
        "",
        f"> {config.DISCLAIMER}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def _jsonable(value: object) -> object:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float) and pd.isna(value):
        return None
    return value


def render_json(result: AnalysisResult) -> str:
    """Machine-readable report: metrics, insights, quality findings, assumptions."""
    payload = {
        "generated_at": result.generated_at.isoformat(),
        "as_of": result.as_of.isoformat() if result.as_of else None,
        "benchmark": {"code": result.benchmark_code, "name": result.benchmark_name},
        "assumptions": result.assumptions,
        "contains_synthetic_data": result.has_synthetic_data,
        "schemes": [_jsonable(s) for s in result.schemes],
        "insights": [_jsonable(i) for i in result.insights],
        "data_quality": {
            "summary": result.quality.summary(),
            "findings": [_jsonable(f) for f in result.quality.sorted_findings()],
            "coverage": result.quality.coverage,
        },
    }
    return json.dumps(payload, indent=2, default=str)


# ---------------------------------------------------------------------------
# Inline SVG charts (no dependencies, no network)
# ---------------------------------------------------------------------------


def _svg_line_chart(
    series_map: dict[str, pd.Series],
    *,
    title: str,
    y_label: str,
    width: int = 900,
    height: int = 340,
    zero_line: bool = False,
) -> str:
    """Render several time series as a single inline SVG line chart.

    Hand-rolled rather than delegated to a plotting library so the published HTML
    stays a single file with zero external requests. Points are decimated to at
    most ~700 per series, which keeps the file small without visible loss.
    """
    series_map = {k: v.dropna() for k, v in series_map.items() if v is not None and len(v) > 1}
    if not series_map:
        return f'<p class="muted">No data available for {html.escape(title)}.</p>'

    pad_l, pad_r, pad_t, pad_b = 62, 16, 18, 34
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b

    x_min = min(s.index[0] for s in series_map.values()).value
    x_max = max(s.index[-1] for s in series_map.values()).value
    y_min = min(float(s.min()) for s in series_map.values())
    y_max = max(float(s.max()) for s in series_map.values())
    if y_max == y_min:
        y_max += 1.0
    span = y_max - y_min
    y_min, y_max = y_min - span * 0.05, y_max + span * 0.05
    x_span = max(x_max - x_min, 1)

    def sx(ts: int) -> float:
        return pad_l + (ts - x_min) / x_span * plot_w

    def sy(value: float) -> float:
        return pad_t + (y_max - value) / (y_max - y_min) * plot_h

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{html.escape(title)}" class="chart">',
        f"<title>{html.escape(title)}</title>",
    ]

    # Horizontal gridlines with value labels.
    for i in range(5):
        value = y_min + (y_max - y_min) * i / 4
        y = sy(value)
        parts.append(
            f'<line class="grid" x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}"/>'
        )
        parts.append(
            f'<text class="axis" x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end">'
            f"{value:,.0f}</text>"
        )
    if zero_line and y_min < 0 < y_max:
        parts.append(
            f'<line class="zero" x1="{pad_l}" y1="{sy(0):.1f}" '
            f'x2="{width - pad_r}" y2="{sy(0):.1f}"/>'
        )

    # Date labels at both ends and the midpoint.
    for fraction in (0.0, 0.5, 1.0):
        ts = x_min + x_span * fraction
        label = pd.Timestamp(int(ts)).strftime("%b %Y")
        anchor = "start" if fraction == 0 else "end" if fraction == 1 else "middle"
        parts.append(
            f'<text class="axis" x="{sx(ts):.1f}" y="{height - 10}" '
            f'text-anchor="{anchor}">{label}</text>'
        )

    legend = []
    for index, (label, series) in enumerate(series_map.items()):
        colour = SERIES_COLOURS[index % len(SERIES_COLOURS)]
        step = max(1, len(series) // 700)
        sampled = series.iloc[::step]
        if sampled.index[-1] != series.index[-1]:
            sampled = pd.concat([sampled, series.iloc[[-1]]])
        points = " ".join(f"{sx(ts.value):.1f},{sy(float(v)):.1f}" for ts, v in sampled.items())
        parts.append(f'<polyline class="line" stroke="{colour}" points="{points}"/>')
        legend.append(
            f'<span class="key"><i style="background:{colour}"></i>{html.escape(label)}</span>'
        )

    parts.append(
        f'<text class="axis-label" x="14" y="{pad_t + plot_h / 2:.0f}" '
        f'transform="rotate(-90 14 {pad_t + plot_h / 2:.0f})" text-anchor="middle">'
        f"{html.escape(y_label)}</text>"
    )
    parts.append("</svg>")
    return (
        f"<figure><figcaption>{html.escape(title)}</figcaption>"
        + "".join(parts)
        + f'<div class="legend">{"".join(legend)}</div></figure>'
    )


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_CSS = """
:root{color-scheme:light dark;--bg:#ffffff;--fg:#16181d;--muted:#5c6270;--line:#e3e6ec;
--card:#f7f8fa;--accent:#2563eb;--crit:#b91c1c;--warn:#b45309;--ok:#047857;}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#9198a1;
--line:#272c34;--card:#161b22;--accent:#58a6ff;--crit:#f85149;--warn:#e3b341;--ok:#3fb950;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,BlinkMacSystemFont,
"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
.wrap{max-width:1120px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:28px;margin:0 0 4px;letter-spacing:-.02em}
h2{font-size:19px;margin:40px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.meta{color:var(--muted);font-size:13px;margin-bottom:20px}
.banner{padding:12px 16px;border-radius:8px;margin:16px 0;border-left:4px solid}
.banner.crit{background:color-mix(in srgb,var(--crit) 12%,transparent);border-color:var(--crit)}
.banner.warn{background:color-mix(in srgb,var(--warn) 12%,transparent);border-color:var(--warn)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin:18px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .label{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.card .value{font-size:24px;font-weight:600;margin-top:4px;letter-spacing:-.02em}
.card .sub{font-size:12px;color:var(--muted);margin-top:2px}
ol.insights{padding-left:20px}
ol.insights li{margin-bottom:10px}
ol.insights b{display:block}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:13.5px;min-width:640px}
th,td{padding:8px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left;white-space:normal;min-width:200px}
thead th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);
border-bottom:2px solid var(--line)}
tbody tr:hover{background:var(--card)}
.pos{color:var(--ok)}.neg{color:var(--crit)}.muted{color:var(--muted)}
.sev-CRITICAL{color:var(--crit);font-weight:600}.sev-WARNING{color:var(--warn)}
.sev-INFO{color:var(--muted)}
figure{margin:20px 0;background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:14px}
figcaption{font-size:13px;font-weight:600;margin-bottom:8px}
svg.chart{width:100%;height:auto;display:block}
.grid{stroke:var(--line);stroke-width:1}
.zero{stroke:var(--muted);stroke-width:1;stroke-dasharray:4 3}
.line{fill:none;stroke-width:1.8;stroke-linejoin:round;stroke-linecap:round}
text.axis{fill:var(--muted);font-size:10px}
text.axis-label{fill:var(--muted);font-size:11px}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin-top:10px;font-size:12px;color:var(--muted)}
.key i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}
footer{margin-top:48px;padding-top:16px;border-top:1px solid var(--line);font-size:12px;
color:var(--muted)}
"""


def _cell(value: float | None, digits: int = 2, suffix: str = "", signed: bool = False) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return '<td class="muted">—</td>'
    cls = ""
    if signed and isinstance(value, (int, float)):
        cls = ' class="pos"' if value >= 0 else ' class="neg"'
    text = f"{value:+,.{digits}f}{suffix}" if signed else f"{value:,.{digits}f}{suffix}"
    return f"<td{cls}>{text}</td>"


def _table(headers: list[str], rows: list[str]) -> str:
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    return (
        f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def render_html(result: AnalysisResult) -> str:
    """Self-contained HTML report with inline SVG charts and no external assets."""
    e = html.escape
    body: list[str] = [
        '<div class="wrap">',
        f"<h1>{e(config.REPORT_TITLE)}</h1>",
        f'<p class="meta">Generated {result.generated_at:%d %b %Y, %H:%M} · '
        f"NAV as of {result.as_of or 'n/a'} · {len(result.schemes)} scheme(s) analysed"
        + (f" · benchmark: {e(result.benchmark_name)}" if result.benchmark_name else "")
        + "</p>",
    ]

    if result.has_synthetic_data:
        body.append(
            '<div class="banner crit"><b>Synthetic data.</b> The live AMFI fetch was '
            "unavailable, so some NAVs here are generated for pipeline testing. They are not "
            "real returns and must not inform any investment decision.</div>"
        )
    if result.quality.has_critical:
        body.append(
            f'<div class="banner warn"><b>{len(result.quality.critical)} critical data-quality '
            f"finding(s).</b> {len(result.quality.unusable_schemes())} scheme(s) were excluded "
            "from the analysis — see Data quality below.</div>"
        )

    # KPI cards.
    if result.schemes:
        by_sharpe = result.ranked_by("sharpe_ratio")
        by_cagr = result.ranked_by("cagr_3y_pct")
        deepest = result.ranked_by("max_drawdown_pct", descending=False)
        cards = []
        if by_cagr:
            cards.append(("Top 3Y CAGR", f"{by_cagr[0].cagr_3y_pct:+.2f}%", by_cagr[0].scheme_name))
        if by_sharpe:
            cards.append(
                ("Best Sharpe", f"{by_sharpe[0].sharpe_ratio:.2f}", by_sharpe[0].scheme_name)
            )
        if deepest:
            cards.append(
                (
                    "Deepest drawdown",
                    f"{deepest[0].max_drawdown_pct:.1f}%",
                    f"{deepest[0].scheme_name} · "
                    + ("recovered" if deepest[0].max_drawdown_recovered else "not recovered"),
                )
            )
        average_vol = metrics.summarise([s.volatility_pct for s in result.schemes])
        if average_vol is not None:
            cards.append(("Average volatility", f"{average_vol:.1f}%", "annualised, daily returns"))
        body.append(
            '<div class="cards">'
            + "".join(
                f'<div class="card"><div class="label">{e(label)}</div>'
                f'<div class="value">{e(value)}</div><div class="sub">{e(sub)}</div></div>'
                for label, value, sub in cards
            )
            + "</div>"
        )

    # Insights.
    body.append("<h2>Executive summary</h2>")
    if result.insights:
        body.append(
            '<ol class="insights">'
            + "".join(f"<li><b>{e(i.headline)}</b>{e(i.detail)}</li>" for i in result.insights)
            + "</ol>"
        )
    else:
        body.append('<p class="muted">No findings.</p>')

    # Charts.
    if result.nav_series:
        analysed_codes = [s.scheme_code for s in result.schemes]
        label_of = {s.scheme_code: s.scheme_name for s in result.schemes}
        if result.benchmark_code and result.benchmark_code in result.nav_series:
            analysed_codes = [*analysed_codes, result.benchmark_code]
            label_of[result.benchmark_code] = f"{result.benchmark_name} (benchmark)"
        chart_series = {
            label_of.get(code, code): result.nav_series[code]
            for code in analysed_codes
            if code in result.nav_series
        }
        common_start = max((s.index[0] for s in chart_series.values()), default=None)
        if common_start is not None:
            rebased = {
                label: metrics.growth_of(series.loc[common_start:], config.GROWTH_CHART_INITIAL)
                for label, series in chart_series.items()
            }
            body.append("<h2>Growth of ₹10,000</h2>")
            body.append(
                _svg_line_chart(
                    rebased,
                    title=f"Growth of ₹10,000 invested on {common_start:%d %b %Y}",
                    y_label="Value (₹)",
                )
            )
            body.append(
                _svg_line_chart(
                    {
                        label: metrics.drawdown_series(series)
                        for label, series in chart_series.items()
                    },
                    title="Drawdown from running peak (%)",
                    y_label="Drawdown (%)",
                    zero_line=True,
                )
            )

    # Tables.
    if result.schemes:
        body.append("<h2>Performance</h2>")
        rows = []
        for s in result.ranked_by("cagr_3y_pct") + [
            s for s in result.schemes if s.cagr_3y_pct is None
        ]:
            rows.append(
                f"<tr><td>{e(s.scheme_name)}<br><span class='muted'>{e(s.fund_house or '')}</span></td>"
                + _cell(s.latest_nav)
                + _cell(s.return_3m_pct, 2, "%", signed=True)
                + _cell(s.return_6m_pct, 2, "%", signed=True)
                + _cell(s.return_1y_pct, 2, "%", signed=True)
                + _cell(s.cagr_3y_pct, 2, "%", signed=True)
                + _cell(s.cagr_5y_pct, 2, "%", signed=True)
                + _cell(s.sip_xirr_3y_pct, 2, "%", signed=True)
                + _cell(s.cagr_since_inception_pct, 2, "%", signed=True)
                + "</tr>"
            )
        body.append(
            _table(
                [
                    "Scheme",
                    "NAV (₹)",
                    "3M",
                    "6M",
                    "1Y",
                    "3Y CAGR",
                    "5Y CAGR",
                    "SIP XIRR 3Y",
                    "Since inception",
                ],
                rows,
            )
        )
        body.append(
            '<p class="muted">Returns under one year are absolute; one year and beyond are '
            "annualised. SIP XIRR reflects a monthly investment schedule.</p>"
        )

        body.append("<h2>Risk &amp; risk-adjusted return</h2>")
        rows = []
        for s in result.ranked_by("sharpe_ratio") or result.schemes:
            recovered = (
                '<td class="muted">—</td>'
                if s.max_drawdown_recovered is None
                else (
                    '<td class="pos">yes</td>'
                    if s.max_drawdown_recovered
                    else '<td class="neg">no</td>'
                )
            )
            rows.append(
                f"<tr><td>{e(s.scheme_name)}</td>"
                + _cell(s.volatility_pct, 2, "%")
                + _cell(s.downside_deviation_pct, 2, "%")
                + _cell(s.sharpe_ratio)
                + _cell(s.sortino_ratio)
                + _cell(s.calmar_ratio)
                + _cell(s.max_drawdown_pct, 1, "%", signed=True)
                + recovered
                + _cell(s.current_drawdown_pct, 1, "%", signed=True)
                + _cell(s.var_95_pct, 2, "%")
                + _cell(s.cvar_95_pct, 2, "%")
                + "</tr>"
            )
        body.append(
            _table(
                [
                    "Scheme",
                    "Volatility",
                    "Downside dev.",
                    "Sharpe",
                    "Sortino",
                    "Calmar",
                    "Max drawdown",
                    "Recovered",
                    "Current DD",
                    "VaR 95%",
                    "CVaR 95%",
                ],
                rows,
            )
        )

        benched = [s for s in result.schemes if s.benchmark]
        if benched:
            body.append(f"<h2>Versus benchmark — {e(result.benchmark_name or '')}</h2>")
            rows = []
            for s in benched:
                b = s.benchmark
                rows.append(
                    f"<tr><td>{e(s.scheme_name)}</td>"
                    + _cell(b.alpha_pct, 2, "%", signed=True)
                    + _cell(b.beta)
                    + _cell(b.r_squared, 3)
                    + _cell(b.tracking_error_pct, 2, "%")
                    + _cell(b.information_ratio)
                    + _cell(b.up_capture_pct, 1, "%")
                    + _cell(b.down_capture_pct, 1, "%")
                    + _cell(b.excess_return_1y_pct, 2, "%", signed=True)
                    + "</tr>"
                )
            body.append(
                _table(
                    [
                        "Scheme",
                        "Alpha (ann.)",
                        "Beta",
                        "R²",
                        "Tracking error",
                        "Information ratio",
                        "Up capture",
                        "Down capture",
                        "1Y excess",
                    ],
                    rows,
                )
            )

        rolling = [s for s in result.schemes if s.rolling_3y]
        if rolling:
            body.append("<h2>Rolling 3-year consistency</h2>")
            rows = []
            for s in rolling:
                r = s.rolling_3y
                rows.append(
                    f"<tr><td>{e(s.scheme_name)}</td><td>{r.observations:,}</td>"
                    + _cell(r.min_pct, 2, "%", signed=True)
                    + _cell(r.median_pct, 2, "%", signed=True)
                    + _cell(r.max_pct, 2, "%", signed=True)
                    + _cell(r.positive_share_pct, 1, "%")
                    + _cell(r.beat_hurdle_share_pct, 1, "%")
                    + "</tr>"
                )
            body.append(
                _table(
                    [
                        "Scheme",
                        "Windows",
                        "Worst",
                        "Median",
                        "Best",
                        "% positive",
                        "% above risk-free",
                    ],
                    rows,
                )
            )

    # Data quality.
    body.append("<h2>Data quality</h2>")
    summary = result.quality.summary()
    body.append(
        f'<p class="muted">{summary["schemes_checked"]} scheme(s) checked · '
        f"{summary['critical']} critical · {summary['warnings']} warning(s) · "
        f"{summary['info']} informational · {summary['schemes_excluded']} excluded</p>"
    )
    findings = result.quality.sorted_findings()
    if findings:
        rows = [
            f'<tr><td><span class="sev-{f.severity}">{f.severity}</span></td>'
            f"<td>{e(f.scheme_code)}</td><td>{e(f.check)}</td><td>{e(f.message)}</td></tr>"
            for f in findings
        ]
        body.append(_table(["Severity", "Scheme", "Check", "Detail"], rows))
    else:
        body.append("<p>All checks passed.</p>")

    # Assumptions.
    body.append("<h2>Methodology &amp; assumptions</h2>")
    body.append(
        _table(
            ["Assumption", "Value"],
            [
                f"<tr><td>{e(k.replace('_', ' ').capitalize())}</td>"
                f'<td style="text-align:left">{e(str(v))}</td></tr>'
                for k, v in result.assumptions.items()
            ],
        )
    )
    body.append(f"<footer>{e(config.DISCLAIMER)}</footer></div>")

    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{e(config.REPORT_TITLE)}</title><style>{_CSS}</style></head><body>"
        + "".join(body)
        + "</body></html>"
    )


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def write_reports(result: AnalysisResult, output_dir: Path | str | None = None) -> dict[str, Path]:
    """Write markdown, HTML, JSON, and the flat CSV. Returns the paths written."""
    directory = Path(output_dir) if output_dir else config.REPORT_DIR
    directory.mkdir(parents=True, exist_ok=True)

    paths = {
        "markdown": directory / "report.md",
        "html": directory / "index.html",
        "json": directory / "report.json",
        "csv": directory / "fund_metrics.csv",
    }
    paths["markdown"].write_text(render_markdown(result), encoding="utf-8")
    paths["html"].write_text(render_html(result), encoding="utf-8")
    paths["json"].write_text(render_json(result), encoding="utf-8")

    frame = result.to_frame()
    if not frame.empty:
        frame.to_csv(paths["csv"], index=False)
        # Keep the historical location working for the notebook and older scripts.
        config.PERFORMANCE_REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(config.PERFORMANCE_REPORT_FILE, index=False)
    else:
        paths.pop("csv")

    logger.info("Reports written to %s", directory)
    return paths


def render_console(result: AnalysisResult, width: int = 100) -> str:
    """Compact terminal summary for interactive pipeline runs."""
    lines = ["=" * width, config.REPORT_TITLE.center(width), "=" * width]
    if result.has_synthetic_data:
        lines.append("!! SYNTHETIC DATA -- not investable analysis !!".center(width))
    frame = result.to_frame()
    if frame.empty:
        lines.append("No schemes analysed.")
    else:
        columns = [
            c
            for c in [
                "scheme_name",
                "latest_nav",
                "return_1y_pct",
                "cagr_3y_pct",
                "volatility_pct",
                "sharpe_ratio",
                "max_drawdown_pct",
                "sma_signal",
            ]
            if c in frame.columns
        ]
        lines.append(frame[columns].to_string(index=False))
    lines.append("-" * width)
    for insight in result.insights[:5]:
        lines.append(f"{insight.rank}. [{insight.category}] {insight.headline}")
    lines.append("=" * width)
    return "\n".join(lines)
