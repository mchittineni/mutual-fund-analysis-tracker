"""
AMFI NAV ingestion via `mftool`, with retries, provenance, and audited runs.

The single most important behaviour here: **synthetic data is opt-in.** The
previous version silently fabricated NAVs whenever the network was unavailable,
which meant a green pipeline could publish invented investment returns. Now the
fallback only runs when the caller passes ``allow_synthetic=True``, and every
synthetic row is tagged ``data_source='synthetic'`` so validation, the report,
and the dashboard all label it loudly.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from src import config, db_manager

logger = logging.getLogger(__name__)

SOURCE_AMFI = "amfi"
SOURCE_SYNTHETIC = "synthetic"

try:  # pragma: no cover - import-time branch depends on the environment
    from mftool import Mftool

    MFTOOL_AVAILABLE = True
except ImportError:  # pragma: no cover
    Mftool = None  # type: ignore[assignment]
    MFTOOL_AVAILABLE = False


class FetchError(RuntimeError):
    """Raised when live data could not be retrieved and synthetic data is not allowed."""


@dataclass
class FetchResult:
    """Outcome of one ingestion run -- returned to the pipeline and audited to SQLite."""

    requested: list[str]
    succeeded: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    synthetic: list[str] = field(default_factory=list)
    rows_written: int = 0
    rows_updated: int = 0

    @property
    def used_synthetic(self) -> bool:
        return bool(self.synthetic)

    @property
    def data_source(self) -> str:
        if self.synthetic and len(self.synthetic) == len(self.requested):
            return SOURCE_SYNTHETIC
        return "mixed" if self.synthetic else SOURCE_AMFI

    def summary(self) -> str:
        parts = [f"{len(self.succeeded)}/{len(self.requested)} schemes fetched"]
        if self.rows_written or self.rows_updated:
            parts.append(f"{self.rows_written} new rows, {self.rows_updated} restated")
        if self.synthetic:
            parts.append(f"SYNTHETIC: {', '.join(self.synthetic)}")
        if self.failed:
            parts.append(f"failed: {', '.join(self.failed)}")
        return "; ".join(parts)


# ---------------------------------------------------------------------------
# Live fetching
# ---------------------------------------------------------------------------


def _with_retries(
    operation: Callable[[], object],
    description: str,
    max_retries: int = config.FETCH_MAX_RETRIES,
    backoff: float = config.FETCH_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> object:
    """Run ``operation`` with exponential backoff and jitter.

    AMFI's endpoint is a public service that rate-limits and occasionally times
    out; jitter avoids synchronised retries when several schemes fail at once.
    """
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            if attempt == max_retries:
                break
            delay = backoff * (2 ** (attempt - 1)) * (0.5 + random.random())
            logger.warning(
                "%s failed (attempt %s/%s): %s -- retrying in %.1fs",
                description,
                attempt,
                max_retries,
                exc,
                delay,
            )
            sleep(delay)
    raise FetchError(f"{description} failed after {max_retries} attempts: {last_error}")


def _parse_history(code: str, history: dict) -> list[tuple[str, str, float]]:
    """Convert an mftool history payload into ``(code, iso_date, nav)`` tuples.

    Malformed individual records are skipped and counted rather than aborting the
    whole scheme -- AMFI occasionally emits 'N.A.' NAVs for non-trading days.
    """
    records: list[tuple[str, str, float]] = []
    skipped = 0
    for record in history.get("data", []):
        try:
            iso_date = pd.to_datetime(record["date"], format="%d-%m-%Y").strftime("%Y-%m-%d")
            nav = float(record["nav"])
            if nav <= 0:
                raise ValueError("non-positive NAV")
            records.append((str(code), iso_date, nav))
        except (KeyError, ValueError, TypeError):
            skipped += 1
    if skipped:
        logger.warning("Scheme %s: skipped %s unparseable NAV records", code, skipped)
    return records


def fetch_scheme(mf, code: str) -> tuple[dict | None, list[tuple[str, str, float]]]:
    """Fetch one scheme's metadata and full NAV history from AMFI."""
    details = _with_retries(lambda: mf.get_scheme_details(code), f"scheme_details({code})")
    history = _with_retries(lambda: mf.get_scheme_historical_nav(code), f"historical_nav({code})")
    if not history or "data" not in history:
        raise FetchError(f"scheme {code}: history payload empty")
    return details, _parse_history(code, history)


# ---------------------------------------------------------------------------
# Synthetic fallback (explicitly opt-in)
# ---------------------------------------------------------------------------

_MOCK_METADATA: dict[str, tuple[str, str, str, str, float]] = {
    "119598": (
        "SBI Bluechip Fund - Direct Plan - Growth",
        "SBI Mutual Fund",
        "Open Ended Schemes",
        "Equity Scheme - Large Cap Fund",
        72.50,
    ),
    "125497": (
        "HDFC Top 100 Fund - Direct Plan - Growth Option",
        "HDFC Mutual Fund",
        "Open Ended Schemes",
        "Equity Scheme - Large Cap Fund",
        840.10,
    ),
    "120503": (
        "Parag Parikh Flexi Cap Fund - Direct Plan - Growth Option",
        "PPFAS Mutual Fund",
        "Open Ended Schemes",
        "Equity Scheme - Flexi Cap Fund",
        75.30,
    ),
    "120716": (
        "UTI Nifty 50 Index Fund - Direct Plan - Growth",
        "UTI Mutual Fund",
        "Open Ended Schemes",
        "Index Fund",
        135.40,
    ),
}


def generate_synthetic_history(
    conn: sqlite3.Connection,
    target_schemes: Sequence[str],
    years: float = 5.0,
    seed: int = 42,
) -> int:
    """Write a deterministic synthetic NAV history for offline development.

    A geometric random walk with a mild upward drift, seeded per scheme so runs
    are reproducible and tests are stable. Rows are tagged ``synthetic`` in the
    database; nothing downstream will present them as real.
    """
    logger.warning(
        "Generating SYNTHETIC NAV history for %s -- for testing only, not investable data",
        ", ".join(target_schemes),
    )
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=int(years * 365.25))
    business_days = pd.bdate_range(start=start_date, end=end_date)
    total = 0

    for code in target_schemes:
        name, house, scheme_type, category, base_nav = _MOCK_METADATA.get(
            str(code),
            (f"Synthetic Scheme {code}", "Synthetic AMC", "Open Ended", "Equity", 100.0),
        )
        db_manager.upsert_scheme_info(
            conn, str(code), name, house, scheme_type, category, SOURCE_SYNTHETIC
        )

        rng = random.Random(f"{seed}-{code}")
        # ~12% annual drift, ~15% annual volatility, expressed per trading day.
        drift, vol = 0.12 / 252, 0.15 / (252**0.5)
        nav = base_nav / ((1 + drift) ** len(business_days))
        records = []
        for day in business_days:
            nav = max(nav * (1 + drift + rng.gauss(0, vol)), 0.01)
            records.append((str(code), day.strftime("%Y-%m-%d"), round(nav, 4)))

        written, _ = db_manager.upsert_nav_records(conn, records, SOURCE_SYNTHETIC)
        total += written
        logger.info("Scheme %s: %s synthetic NAV rows", code, len(records))
    return total


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def fetch_and_store_funds(
    target_schemes: Sequence[str] | None = None,
    conn: sqlite3.Connection | None = None,
    *,
    allow_synthetic: bool = False,
    polite_delay: float = config.FETCH_POLITE_DELAY_SECONDS,
) -> FetchResult:
    """Fetch every requested scheme into SQLite and return an audited result.

    Per-scheme failures are collected rather than fatal: two good schemes still
    produce a report, with the third listed as failed. The run only raises
    ``FetchError`` when *nothing* could be fetched and synthetic data is off.
    """
    schemes = [str(code) for code in (target_schemes or config.DEFAULT_TARGET_SCHEMES)]
    result = FetchResult(requested=schemes)

    owns_connection = conn is None
    if conn is None:
        conn = db_manager.setup_database()

    run_id = db_manager.start_run(conn, schemes)
    try:
        if not MFTOOL_AVAILABLE:
            message = "mftool is not installed (pip install -r requirements.txt)"
            if not allow_synthetic:
                raise FetchError(f"{message}; refusing to fabricate NAV data")
            logger.warning("%s -- falling back to synthetic data", message)
            result.rows_written = generate_synthetic_history(conn, schemes)
            result.synthetic = list(schemes)
            result.succeeded = list(schemes)
        else:
            mf = Mftool()
            for index, code in enumerate(schemes):
                logger.info("Fetching AMFI scheme %s (%s/%s)", code, index + 1, len(schemes))
                try:
                    details, records = fetch_scheme(mf, code)
                    if details:
                        db_manager.upsert_scheme_info(
                            conn,
                            str(details.get("scheme_code", code)),
                            str(details.get("scheme_name") or f"Scheme {code}"),
                            details.get("fund_house"),
                            details.get("scheme_type"),
                            details.get("scheme_category"),
                            SOURCE_AMFI,
                        )
                    written, updated = db_manager.upsert_nav_records(conn, records, SOURCE_AMFI)
                    result.rows_written += written
                    result.rows_updated += updated
                    result.succeeded.append(code)
                    logger.info(
                        "Scheme %s: %s records (%s new, %s restated)",
                        code,
                        len(records),
                        written,
                        updated,
                    )
                except Exception as exc:
                    logger.error("Scheme %s failed: %s", code, exc)
                    result.failed[code] = str(exc)
                    if allow_synthetic:
                        result.rows_written += generate_synthetic_history(conn, [code])
                        result.synthetic.append(code)
                if index < len(schemes) - 1 and polite_delay:
                    time.sleep(polite_delay)  # be a good citizen of AMFI's servers

        if not result.succeeded and not result.synthetic:
            raise FetchError(
                "No scheme could be fetched: "
                + "; ".join(f"{k}: {v}" for k, v in result.failed.items())
            )

        db_manager.finish_run(
            conn,
            run_id,
            rows_written=result.rows_written,
            rows_updated=result.rows_updated,
            data_source=result.data_source,
            status="success" if not result.failed else "partial",
            error=None if not result.failed else str(result.failed),
        )
        logger.info("Ingestion complete: %s", result.summary())
        return result
    except Exception as exc:
        db_manager.finish_run(conn, run_id, status="failed", error=str(exc))
        raise
    finally:
        if owns_connection:
            conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    fetch_and_store_funds()
