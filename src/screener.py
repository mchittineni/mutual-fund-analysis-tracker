"""
Screening and scoring across the fund universe.

Two jobs, deliberately kept in one place because the second is only defensible
given the first:

* **Screen** -- filter the universe by category, fund house, and metric floors
  and ceilings, and sort by any metric. Mechanical, and it makes no claims.
* **Score** -- reduce several metrics to one number, so a hundred funds can be
  ordered at a glance.

A composite score is the most dangerous thing in this codebase, because a single
number invites a decision while hiding everything that produced it. Three rules
keep it honest:

1. **The components are always visible.** Every score column ships with its
   `score_*` component columns. A score of 68 that comes from 95 on returns and
   12 on drawdown is a different fund from one that scores 68 on every component,
   and the reader must be able to tell them apart. `explain_score()` renders
   exactly that breakdown for one scheme.
2. **The inputs are category percentiles, not raw values.** Scoring a liquid
   fund's 6% return against a small-cap fund's 24% would rank the entire debt
   universe last for doing precisely its job. Percentiles come from `peers`,
   which ranks within AMFI's own categories and inverts the metrics where lower
   is better.
3. **A thin peer set produces no score at all.** Below `peers.MIN_PEERS`
   comparable funds the percentile is noise, so the score is absent rather than a
   confident-looking number. Missing components are dropped and the weights
   renormalised, and `score_components` records how many actually contributed.

The weights are editorial, not derived -- they encode "returns and risk-adjusted
returns matter most, drawdown next, consistency last". They are exposed as
`DEFAULT_WEIGHTS` and overridable per call precisely because reasonable people
weight them differently. No weighting makes this investment advice.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from src import config, db_manager, peers

logger = logging.getLogger(__name__)

# Each component maps to the metrics whose category percentiles drive it. Those
# percentiles are already direction-corrected by `peers`, so 100 is always good.
COMPONENT_METRICS: dict[str, tuple[str, ...]] = {
    # What the fund actually returned, over the horizons most people hold for.
    "returns": ("return_1y_pct", "cagr_3y_pct", "cagr_5y_pct"),
    # Return per unit of risk taken to get it.
    "risk_adjusted": ("sharpe_ratio", "sortino_ratio", "calmar_ratio"),
    # How far it fell, and how violently it moves.
    "drawdown": ("max_drawdown_pct", "volatility_pct"),
    # What a monthly investor actually experienced, which differs from lump-sum.
    "consistency": ("sip_xirr_3y_pct",),
}

DEFAULT_WEIGHTS: dict[str, float] = {
    "returns": 0.35,
    "risk_adjusted": 0.35,
    "drawdown": 0.20,
    "consistency": 0.10,
}

# Metrics a caller can express as a floor or a ceiling.
FILTERABLE = (
    "return_1y_pct",
    "cagr_3y_pct",
    "cagr_5y_pct",
    "sharpe_ratio",
    "sortino_ratio",
    "calmar_ratio",
    "volatility_pct",
    "max_drawdown_pct",
    "sip_xirr_3y_pct",
    "observations",
    "history_years",
)


@dataclass(frozen=True)
class ScoreBreakdown:
    """One fund's composite score with the components that produced it."""

    scheme_code: str
    scheme_name: str
    category: str
    score: float | None
    components: dict[str, float]
    weights: dict[str, float]
    peers: int

    def explain(self) -> str:
        """A human-readable breakdown. The score never travels without this."""
        if self.score is None:
            return (
                f"{self.scheme_name} ({self.scheme_code}): no score -- only {self.peers} "
                f"comparable fund(s) in {self.category}, and {peers.MIN_PEERS} are needed "
                "before a percentile means anything."
            )
        # The weights shown are the *effective* ones -- renormalised over the
        # components this fund actually has -- so the contributions add up to the
        # score printed above them. Showing the nominal weights instead would
        # leave the reader with a table that does not reconcile.
        total = sum(self.weights.get(name, 0.0) for name in self.components) or 1.0
        lines = [
            f"{self.scheme_name} ({self.scheme_code}) scores {self.score:.1f}/100 "
            f"against {self.peers} peers in {self.category}.",
            "",
            "| Component | Category percentile | Weight | Contribution |",
            "|:--|---:|---:|---:|",
        ]
        for name, value in sorted(self.components.items(), key=lambda kv: -kv[1]):
            weight = self.weights.get(name, 0.0) / total
            lines.append(f"| {name} | {value:.0f} | {weight:.0%} | {value * weight:.1f} |")
        lines.append(f"| **Score** | | **100%** | **{self.score:.1f}** |")

        missing = set(COMPONENT_METRICS) - set(self.components)
        if missing:
            lines += [
                "",
                f"No data for: {', '.join(sorted(missing))}. The remaining weights were "
                f"renormalised over the {len(self.components)} available component(s) "
                "rather than treating the gap as a zero, which would have been a silent "
                "penalty for a fund that is merely young.",
            ]
        return "\n".join(lines)


def _percentile_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Add a `pct_<metric>` column for every ranked metric, within this frame.

    The caller is responsible for the frame being one category -- percentiles
    across categories would compare a liquid fund to a small-cap fund.
    """
    out = frame.copy()
    for metric in peers.RANKED_METRICS:
        if metric not in out.columns:
            continue
        series = pd.to_numeric(out[metric], errors="coerce")
        if series.notna().sum() < peers.MIN_PEERS:
            continue
        higher_is_better = metric not in peers.LOWER_IS_BETTER
        out[f"pct_{metric}"] = [
            peers.percentile_of(series, value, higher_is_better) if pd.notna(value) else np.nan
            for value in series
        ]
    return out


def score_frame(frame: pd.DataFrame, weights: dict[str, float] | None = None) -> pd.DataFrame:
    """Score every row of a single-category metric frame.

    Returns the frame with `score` plus one `score_<component>` column per
    component. The components are not optional extras: they are the only way a
    reader can see whether a score came from returns or from a shallow drawdown.
    """
    weights = weights or DEFAULT_WEIGHTS
    if frame.empty:
        return frame

    ranked = _percentile_frame(frame)
    if len(ranked) < peers.MIN_PEERS:
        # Too few funds for a percentile to mean anything, so no score at all.
        ranked["score"] = np.nan
        for component in COMPONENT_METRICS:
            ranked[f"score_{component}"] = np.nan
        ranked["score_components"] = 0
        return ranked

    component_values: dict[str, pd.Series] = {}
    for component, metrics_used in COMPONENT_METRICS.items():
        columns = [f"pct_{m}" for m in metrics_used if f"pct_{m}" in ranked.columns]
        if columns:
            # Mean of whichever horizons exist: a three-year-old fund is scored on
            # what it has, not penalised to zero for lacking a five-year figure.
            component_values[component] = ranked[columns].mean(axis=1, skipna=True)
        ranked[f"score_{component}"] = component_values.get(component, np.nan)

    if not component_values:
        ranked["score"] = np.nan
        ranked["score_components"] = 0
        return ranked

    values = pd.DataFrame(component_values)
    applied = pd.Series({name: weights.get(name, 0.0) for name in values.columns})
    # Renormalise per row over the components that fund actually has, so a missing
    # metric dilutes nothing -- treating it as zero would be a silent penalty.
    mask = values.notna()
    weight_sum = mask.mul(applied, axis=1).sum(axis=1)
    weighted = values.fillna(0.0).mul(applied, axis=1).sum(axis=1)

    ranked["score"] = pd.to_numeric(
        weighted.divide(weight_sum.where(weight_sum > 0)), errors="coerce"
    ).round(1)
    ranked["score_components"] = mask.sum(axis=1)
    return ranked


def score_category(
    db_path: str | Path | None = None,
    *,
    category: str,
    risk_free_rate: float = config.RISK_FREE_RATE,
    weights: dict[str, float] | None = None,
) -> pd.DataFrame:
    """Compute and score every analysable fund in one AMFI category."""
    frame = peers.category_metrics(db_path, category=category, risk_free_rate=risk_free_rate)
    if frame.empty:
        return frame
    scored = score_frame(frame, weights)
    return scored.sort_values("score", ascending=False, na_position="last")


def explain_score(scored: pd.DataFrame, scheme_code: str) -> ScoreBreakdown | None:
    """Pull one fund's score apart into the components that produced it."""
    row = scored.loc[scored["scheme_code"].astype(str) == str(scheme_code)]
    if row.empty:
        return None
    record = row.iloc[0]

    components = {
        component: float(record[f"score_{component}"])
        for component in COMPONENT_METRICS
        if f"score_{component}" in record.index and pd.notna(record[f"score_{component}"])
    }
    score = record.get("score")
    return ScoreBreakdown(
        scheme_code=str(record["scheme_code"]),
        scheme_name=str(record.get("scheme_name") or record["scheme_code"]),
        category=str(record.get("scheme_category") or "unknown"),
        score=None if pd.isna(score) else float(score),
        components=components,
        weights=DEFAULT_WEIGHTS,
        peers=len(scored),
    )


def screen(
    db_path: str | Path | None = None,
    *,
    category: str | None = None,
    fund_house: str | None = None,
    query: str | None = None,
    minimums: dict[str, float] | None = None,
    maximums: dict[str, float] | None = None,
    sort_by: str = "score",
    ascending: bool = False,
    limit: int = 50,
    risk_free_rate: float = config.RISK_FREE_RATE,
    weights: dict[str, float] | None = None,
) -> pd.DataFrame:
    """Filter and rank the analysable universe.

    Scoring happens **within each category, and only then are the results
    combined**, which is the only way a cross-category screen can be honest: a
    liquid fund scoring 90 means "excellent liquid fund", not "beats an equity
    fund".

    ``minimums``/``maximums`` are metric floors and ceilings, e.g.
    ``minimums={"cagr_3y_pct": 12}, maximums={"volatility_pct": 18}``.
    """
    if category:
        categories = [category]
    else:
        universe = db_manager.search_schemes(db_path, with_history_only=True, limit=10_000)
        if universe.empty:
            return pd.DataFrame()
        categories = [str(name) for name in universe["scheme_category"].dropna().unique()]
    if not categories:
        return pd.DataFrame()

    frames = [
        scored
        for scored in (
            score_category(db_path, category=name, risk_free_rate=risk_free_rate, weights=weights)
            for name in categories
        )
        if not scored.empty
    ]
    if not frames:
        return pd.DataFrame()

    frame = pd.concat(frames, ignore_index=True)

    if fund_house:
        frame = frame[
            frame["fund_house"].astype(str).str.contains(fund_house, case=False, na=False)
        ]
    if query:
        frame = frame[frame["scheme_name"].astype(str).str.contains(query, case=False, na=False)]

    for metric, floor in (minimums or {}).items():
        if metric in frame.columns:
            frame = frame[pd.to_numeric(frame[metric], errors="coerce") >= floor]
    for metric, ceiling in (maximums or {}).items():
        if metric in frame.columns:
            frame = frame[pd.to_numeric(frame[metric], errors="coerce") <= ceiling]

    if sort_by in frame.columns:
        frame = frame.sort_values(sort_by, ascending=ascending, na_position="last")
    else:
        logger.warning("Cannot sort by %r; it is not a column in the screen", sort_by)

    return frame.head(limit).reset_index(drop=True)


def summarise(frame: pd.DataFrame, top: int = 10) -> str:
    """Render a screen as Markdown, with the score components beside the score."""
    if frame.empty:
        return "No fund matched the screen."

    columns = [
        ("scheme_name", "Fund", "{}"),
        ("scheme_category", "Category", "{}"),
        ("score", "Score", "{:.1f}"),
        ("score_returns", "Returns", "{:.0f}"),
        ("score_risk_adjusted", "Risk-adj", "{:.0f}"),
        ("score_drawdown", "Drawdown", "{:.0f}"),
        ("score_consistency", "SIP", "{:.0f}"),
        ("cagr_3y_pct", "3y CAGR %", "{:.2f}"),
        ("volatility_pct", "Vol %", "{:.2f}"),
    ]
    present = [column for column in columns if column[0] in frame.columns]

    lines = [
        "| " + " | ".join(label for _, label, _ in present) + " |",
        "|" + "|".join([":--"] + ["---:"] * (len(present) - 1)) + "|",
    ]
    for _, row in frame.head(top).iterrows():
        cells = []
        for key, _, fmt in present:
            value = row[key]
            # An em dash, never a zero: a missing metric is not a bad metric.
            cells.append("—" if pd.isna(value) else fmt.format(value))
        lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        "Scores are category percentiles (0-100, higher is better) weighted "
        + ", ".join(f"{name} {weight:.0%}" for name, weight in DEFAULT_WEIGHTS.items())
        + ". They rank funds *within* their AMFI category, so a score is not "
        "comparable across categories, and it is not advice.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_bounds(values: Sequence[str] | None, label: str) -> dict[str, float]:
    """Turn ``--min cagr_3y_pct=12`` pairs into a dict, rejecting unknown metrics."""
    bounds: dict[str, float] = {}
    for item in values or []:
        metric, _, raw = item.partition("=")
        metric = metric.strip()
        if not raw:
            raise argparse.ArgumentTypeError(f"--{label} expects METRIC=VALUE, got {item!r}")
        if metric not in FILTERABLE:
            raise argparse.ArgumentTypeError(
                f"{metric!r} cannot be filtered on. Available: {', '.join(FILTERABLE)}"
            )
        try:
            bounds[metric] = float(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"--{label} {metric} needs a number, got {raw!r}"
            ) from None
    return bounds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.screener",
        description=(
            "Screen and score the stored fund universe. Scores are category "
            "percentiles, so they rank a fund among its peers, not across categories."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--db-path", default=None, help="Override the SQLite database path")
    parser.add_argument("--category", default=None, help="Restrict to one AMFI category")
    parser.add_argument("--fund-house", default=None, help="Substring match on the AMC name")
    parser.add_argument("--query", default=None, help="Substring match on the scheme name")
    parser.add_argument(
        "--min",
        dest="minimums",
        action="append",
        metavar="METRIC=VALUE",
        help="Floor for a metric, e.g. --min cagr_3y_pct=12 (repeatable)",
    )
    parser.add_argument(
        "--max",
        dest="maximums",
        action="append",
        metavar="METRIC=VALUE",
        help="Ceiling for a metric, e.g. --max volatility_pct=18 (repeatable)",
    )
    parser.add_argument("--sort-by", default="score", help="Column to sort by")
    parser.add_argument("--ascending", action="store_true", help="Sort ascending")
    parser.add_argument("--limit", type=int, default=25, help="Rows to return")
    parser.add_argument(
        "--explain",
        default=None,
        metavar="SCHEME_CODE",
        help="Print the score breakdown for one scheme instead of the table",
    )
    parser.add_argument("--csv", default=None, help="Also write the full screen to this CSV")
    parser.add_argument(
        "--summary-file",
        default=None,
        help="Append the Markdown table here (point at $GITHUB_STEP_SUMMARY in Actions)",
    )
    parser.add_argument(
        "--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")

    try:
        minimums = _parse_bounds(args.minimums, "min")
        maximums = _parse_bounds(args.maximums, "max")
    except argparse.ArgumentTypeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    frame = screen(
        args.db_path,
        category=args.category,
        fund_house=args.fund_house,
        query=args.query,
        minimums=minimums,
        maximums=maximums,
        sort_by=args.sort_by,
        ascending=args.ascending,
        limit=args.limit,
    )

    if frame.empty:
        print(
            "No fund matched the screen. If the database is new, run "
            "`python -m src.catalogue --backfill 100` to fill in some history first."
        )
        return 0

    if args.explain:
        breakdown = explain_score(frame, args.explain)
        if breakdown is None:
            print(f"{args.explain} is not in the screen results.", file=sys.stderr)
            return 1
        print(breakdown.explain())
    else:
        print(summarise(frame, top=args.limit))

    if args.csv:
        path = Path(args.csv)
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(path, index=False)
        print(f"\nFull screen written to {path}")

    if args.summary_file:
        try:
            path = Path(args.summary_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write("## Fund screen\n\n" + summarise(frame, top=args.limit) + "\n")
        except (OSError, ValueError) as exc:
            logger.warning("Could not write the summary: %s", exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
