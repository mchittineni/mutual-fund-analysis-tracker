"""
Category-relative ranking: how a fund compares to the funds it actually competes with.

A 14% CAGR means nothing on its own. It is excellent for a large-cap fund in a flat
year and mediocre for a small-cap fund in a bull run. The benchmark in
`metrics.compare_to_benchmark()` answers "did it beat the index?"; this module
answers the different and often more useful question: **"where does it sit among
its peers?"**

The unit of comparison is AMFI's own scheme category (`Equity Scheme - Large Cap
Fund`, `Debt Scheme - Liquid Fund`, ...), which arrives with the full-universe
catalogue. That matters: the categories are the regulator's, not ours, so the peer
set is defensible rather than a judgement call we made.

Two deliberate restraints:

* A percentile computed against three peers is noise dressed as precision, so a
  category needs `MIN_PEERS` members with analysable history before any figure is
  reported.
* Percentiles are reported per metric, never blended into one score. A composite
  hides exactly the trade-off the reader needs to see -- a fund can sit in the
  90th percentile for return and the 10th for drawdown, and averaging those two
  into "50th" describes no fund that exists.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from src import config, db_manager, metrics

logger = logging.getLogger(__name__)

# Below this, a percentile is not a statistic.
MIN_PEERS = 5

# Metrics where a *lower* value is better, so the percentile must be inverted:
# being in the 90th percentile for volatility is bad news, and reporting it
# alongside "90th percentile for return" without flipping it would be misleading.
LOWER_IS_BETTER = {
    "volatility_pct",
    "downside_deviation_pct",
    "var_95_pct",
    "cvar_95_pct",
    "tracking_error_pct",
}

# Drawdown is negative, so a larger (closer to zero) value is better and the
# natural ordering already works.
RANKED_METRICS = (
    "return_1y_pct",
    "cagr_3y_pct",
    "cagr_5y_pct",
    "sharpe_ratio",
    "sortino_ratio",
    "calmar_ratio",
    "volatility_pct",
    "max_drawdown_pct",
    "sip_xirr_3y_pct",
)


@dataclass(frozen=True)
class PeerRank:
    """Where one scheme sits among its category peers for one metric."""

    metric: str
    value: float
    percentile: float
    rank: int
    peers: int
    category_median: float
    higher_is_better: bool

    @property
    def quartile(self) -> int:
        """1 = best quartile, 4 = worst. Percentiles are already direction-corrected."""
        return min(4, int((100 - self.percentile) // 25) + 1)

    def describe(self) -> str:
        direction = "lower is better" if not self.higher_is_better else "higher is better"
        return (
            f"{self.metric}: {self.value:.2f} ranks {self.rank} of {self.peers} "
            f"({self.percentile:.0f}th percentile, Q{self.quartile}; "
            f"category median {self.category_median:.2f}, {direction})"
        )


@dataclass
class PeerComparison:
    """All category-relative rankings for one scheme."""

    scheme_code: str
    category: str
    peers: int
    ranks: dict[str, PeerRank]

    def top_quartile_metrics(self) -> list[str]:
        return [name for name, rank in self.ranks.items() if rank.quartile == 1]

    def bottom_quartile_metrics(self) -> list[str]:
        return [name for name, rank in self.ranks.items() if rank.quartile == 4]

    def as_row(self) -> dict[str, object]:
        row: dict[str, object] = {
            "scheme_code": self.scheme_code,
            "peer_category": self.category,
            "peer_count": self.peers,
        }
        for name, rank in self.ranks.items():
            row[f"pct_{name}"] = round(rank.percentile, 1)
        return row


def percentile_of(values: pd.Series, value: float, higher_is_better: bool = True) -> float:
    """Percentile of ``value`` within ``values``, corrected for metric direction.

    Uses the *mean* of the strictly-below and at-or-below fractions, which is the
    standard mid-rank treatment of ties: with ten identical funds, every one sits
    at the 50th percentile rather than all at the 100th or all at the 0th.
    """
    clean = values.dropna().to_numpy(dtype=float)
    if clean.size == 0:
        return float("nan")
    if not higher_is_better:
        clean, value = -clean, -value
    below = float(np.sum(clean < value))
    at_or_below = float(np.sum(clean <= value))
    return (below + at_or_below) / 2.0 / clean.size * 100.0


def rank_within(
    frame: pd.DataFrame, scheme_code: str, metric: str, higher_is_better: bool = True
) -> PeerRank | None:
    """Rank one scheme's metric against every peer in ``frame``."""
    if metric not in frame.columns:
        return None
    series = pd.to_numeric(frame[metric], errors="coerce")
    peers = series.dropna()
    if len(peers) < MIN_PEERS:
        return None

    row = frame.loc[frame["scheme_code"].astype(str) == str(scheme_code)]
    if row.empty or pd.isna(row[metric].iloc[0]):
        return None
    value = float(row[metric].iloc[0])

    percentile = percentile_of(peers, value, higher_is_better)
    ordered = peers.sort_values(ascending=not higher_is_better)
    rank = int((ordered.to_numpy() == value).argmax()) + 1

    return PeerRank(
        metric=metric,
        value=value,
        percentile=percentile,
        rank=rank,
        peers=len(peers),
        category_median=float(peers.median()),
        higher_is_better=higher_is_better,
    )


def compare_within_category(
    frame: pd.DataFrame, scheme_code: str, category: str
) -> PeerComparison | None:
    """Rank a scheme against a frame of peer metrics from the same category."""
    ranks: dict[str, PeerRank] = {}
    for metric in RANKED_METRICS:
        rank = rank_within(
            frame, scheme_code, metric, higher_is_better=metric not in LOWER_IS_BETTER
        )
        if rank is not None:
            ranks[metric] = rank
    if not ranks:
        return None
    return PeerComparison(
        scheme_code=str(scheme_code),
        category=category,
        peers=max(rank.peers for rank in ranks.values()),
        ranks=ranks,
    )


def category_metrics(
    db_path: str | Path | None = None,
    *,
    category: str,
    risk_free_rate: float = config.RISK_FREE_RATE,
    min_observations: int = config.MIN_OBSERVATIONS,
    max_schemes: int = 400,
) -> pd.DataFrame:
    """Compute the metric table for every analysable scheme in one AMFI category.

    Only schemes with enough stored history qualify -- the catalogue knows about
    ~14,000 schemes, but a peer set is built from the ones actually backfilled.
    That makes coverage visible instead of silently thin: `peer_count` in the
    output tells the reader how many funds the percentile is computed against.
    """
    frame = db_manager.load_data(db_path)
    if frame.empty:
        return pd.DataFrame()

    frame = frame[frame["scheme_category"] == category]
    if frame.empty:
        return pd.DataFrame()

    counts = frame.groupby("scheme_code")["date"].count()
    eligible = counts[counts >= min_observations].sort_values(ascending=False)
    if eligible.empty:
        return pd.DataFrame()
    eligible = eligible.head(max_schemes)

    rows = []
    for code in eligible.index:
        group = frame[frame["scheme_code"] == code]
        nav = metrics.to_nav_series(group)
        if nav.empty:
            continue
        meta = group.iloc[-1]
        try:
            computed = metrics.compute_scheme_metrics(
                nav,
                scheme_code=str(code),
                scheme_name=str(meta["scheme_name"]),
                fund_house=(None if pd.isna(meta.get("fund_house")) else str(meta["fund_house"])),
                scheme_category=category,
                data_source=str(meta.get("data_source") or "unknown"),
                annual_risk_free=risk_free_rate,
            )
        except ValueError:
            continue
        rows.append(computed.as_row())

    logger.info("Category %r: %s analysable scheme(s)", category, len(rows))
    return pd.DataFrame(rows)


def peer_comparison(
    db_path: str | Path | None = None,
    *,
    scheme_code: str,
    category: str,
    risk_free_rate: float = config.RISK_FREE_RATE,
) -> PeerComparison | None:
    """Convenience wrapper: build the peer set from the database, then rank."""
    frame = category_metrics(db_path, category=category, risk_free_rate=risk_free_rate)
    if frame.empty or len(frame) < MIN_PEERS:
        logger.info(
            "Category %r has %s analysable scheme(s); %s needed for a percentile",
            category,
            len(frame),
            MIN_PEERS,
        )
        return None
    return compare_within_category(frame, scheme_code, category)


def category_summary(frame: pd.DataFrame) -> dict[str, float | int]:
    """Headline distribution for a category's metric table."""
    if frame.empty:
        return {}
    summary: dict[str, float | int] = {"schemes": len(frame)}
    for metric in ("cagr_3y_pct", "sharpe_ratio", "volatility_pct", "max_drawdown_pct"):
        if metric in frame:
            series = pd.to_numeric(frame[metric], errors="coerce").dropna()
            if not series.empty:
                summary[f"{metric}_median"] = round(float(series.median()), 2)
                summary[f"{metric}_best"] = round(
                    float(series.min() if metric in LOWER_IS_BETTER else series.max()), 2
                )
    return summary
