"""
Full-universe catalogue ingestion from AMFI's consolidated NAV file.

AMFI publishes every scheme it knows about in one file, `NAVAll.txt`, structured
as sections:

    Scheme Code;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date

    Open Ended Schemes(Equity Scheme - Large Cap Fund)
    SBI Mutual Fund
    119598;INF200K01QX4;-;SBI Bluechip Fund - Direct Plan - Growth;104.8;12-Aug-2026

`mftool.get_scheme_codes()` fetches this same file but keeps only the code and
name, discarding the section headers -- which is where the **scheme type, the
category, and the fund house** live. Parsing it ourselves gets all of that plus
the ISINs and the day's NAV from a single HTTP request, instead of ~14,000
per-scheme calls.

That difference is what makes a full-universe catalogue practical:

* **Catalogue** (this module): one request, every scheme, metadata + today's NAV.
  Cheap enough to run daily, and running it daily accumulates a NAV history for
  the entire universe over time.
* **History** (`fetch_amfi_data`): one request *per scheme* for its full NAV
  history. Expensive, so it is reserved for schemes actually being analysed, and
  `backfill_history()` fills the rest incrementally within a budget.
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from src import config, db_manager, fetch_amfi_data

logger = logging.getLogger(__name__)

NAVALL_URL = "https://www.amfiindia.com/spages/NAVAll.txt"

# "Open Ended Schemes(Equity Scheme - Large Cap Fund)" -> type, category.
_SECTION_RE = re.compile(r"^(?P<type>[A-Za-z ]*Schemes?)\s*\((?P<category>.+)\)\s*$")
_MISSING = {"", "-", "n.a.", "na", "n/a", "null", "none"}


@dataclass(frozen=True)
class CatalogueEntry:
    """One scheme as AMFI describes it in the consolidated file."""

    scheme_code: str
    scheme_name: str
    fund_house: str | None
    scheme_type: str | None
    scheme_category: str | None
    isin_growth: str | None = None
    isin_reinvest: str | None = None
    nav: float | None = None
    nav_date: str | None = None  # ISO


@dataclass
class CatalogueResult:
    """Outcome of a catalogue refresh."""

    parsed: int = 0
    schemes_written: int = 0
    navs_written: int = 0
    categories: int = 0
    fund_houses: int = 0
    skipped_lines: int = 0
    nav_date: str | None = None
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.parsed:,} schemes parsed across {self.categories} categories and "
            f"{self.fund_houses} fund houses; {self.schemes_written:,} metadata rows and "
            f"{self.navs_written:,} NAV rows written"
            + (f" (as of {self.nav_date})" if self.nav_date else "")
        )


def _clean(value: str | None) -> str | None:
    """Normalise AMFI's several spellings of 'missing' into ``None``."""
    if value is None:
        return None
    text = value.strip()
    return None if text.lower() in _MISSING else text


def _parse_nav(value: str | None) -> float | None:
    text = _clean(value)
    if text is None:
        return None
    try:
        nav = float(text)
    except ValueError:
        return None
    return nav if nav > 0 else None


def _parse_date(value: str | None) -> str | None:
    """AMFI dates look like ``12-Aug-2026``. Returns ISO, or ``None``."""
    text = _clean(value)
    if text is None:
        return None
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_navall(text: str) -> tuple[list[CatalogueEntry], int]:
    """Parse the consolidated NAV file into entries plus a skipped-line count.

    The format is positional: a section header sets the type and category for the
    scheme lines that follow, and a bare line with no semicolons is a fund house.
    Lines that parse to neither are counted rather than raised on -- AMFI adds
    notices and blank sections, and one odd line must not cost 14,000 schemes.
    """
    entries: list[CatalogueEntry] = []
    skipped = 0
    scheme_type: str | None = None
    scheme_category: str | None = None
    fund_house: str | None = None
    seen: set[str] = set()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if ";" not in line:
            section = _SECTION_RE.match(line)
            if section:
                scheme_type = section.group("type").strip()
                scheme_category = section.group("category").strip()
                # A new section resets the fund house; the file always names the
                # AMC again before its schemes.
                fund_house = None
            else:
                fund_house = line
            continue

        fields = [part.strip() for part in line.split(";")]
        if len(fields) < 5:
            skipped += 1
            continue

        code = _clean(fields[0])
        name = _clean(fields[3])
        # The header row and any repeat of it.
        if not code or not code.isdigit() or not name:
            skipped += 1
            continue
        if code in seen:
            skipped += 1
            continue
        seen.add(code)

        entries.append(
            CatalogueEntry(
                scheme_code=code,
                scheme_name=name,
                fund_house=fund_house,
                scheme_type=scheme_type,
                scheme_category=scheme_category,
                isin_growth=_clean(fields[1]),
                isin_reinvest=_clean(fields[2]),
                nav=_parse_nav(fields[4]),
                nav_date=_parse_date(fields[5]) if len(fields) > 5 else None,
            )
        )

    return entries, skipped


def fetch_navall(url: str = NAVALL_URL, timeout: float = 60.0) -> str:
    """Download the consolidated NAV file.

    Uses `requests` directly rather than mftool: this needs the whole file, not
    mftool's code-and-name reduction of it. A bounded timeout is mandatory --
    mftool's own session has none, which is how an unreachable AMFI became a
    75-second hang elsewhere in this codebase.
    """
    import requests

    if not fetch_amfi_data.amfi_reachable():
        raise fetch_amfi_data.FetchError(
            f"{config.AMFI_HOST} is not reachable; cannot refresh the catalogue"
        )
    logger.info("Downloading the AMFI catalogue from %s", url)
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    if len(response.text) < 1000:
        raise fetch_amfi_data.FetchError(
            f"Catalogue response was implausibly short ({len(response.text)} bytes)"
        )
    return response.text


def refresh_catalogue(
    conn: sqlite3.Connection,
    *,
    text: str | None = None,
    store_navs: bool = True,
    url: str = NAVALL_URL,
) -> CatalogueResult:
    """Ingest the full scheme universe, and optionally today's NAV for each.

    ``store_navs`` is what turns a daily run into a growing history for every
    fund in India: each run appends one NAV row per scheme. Pass ``text`` to
    parse a file you already have (used by the tests, and useful for replaying a
    saved snapshot).
    """
    started = time.monotonic()
    payload = text if text is not None else fetch_navall(url)
    entries, skipped = parse_navall(payload)

    result = CatalogueResult(parsed=len(entries), skipped_lines=skipped)
    if not entries:
        result.errors.append("The catalogue parsed to zero schemes; the format may have changed")
        result.duration_seconds = time.monotonic() - started
        return result

    result.categories = len({e.scheme_category for e in entries if e.scheme_category})
    result.fund_houses = len({e.fund_house for e in entries if e.fund_house})
    result.schemes_written = db_manager.upsert_catalogue(conn, entries)

    if store_navs:
        nav_rows = [
            (e.scheme_code, e.nav_date, e.nav)
            for e in entries
            if e.nav is not None and e.nav_date is not None
        ]
        written, updated = db_manager.upsert_nav_records(
            conn, nav_rows, fetch_amfi_data.SOURCE_AMFI
        )
        result.navs_written = written + updated
        dates = {row[1] for row in nav_rows}
        result.nav_date = max(dates) if dates else None

    result.duration_seconds = time.monotonic() - started
    logger.info("Catalogue refresh complete: %s", result.summary())
    return result


# ---------------------------------------------------------------------------
# Incremental history backfill
# ---------------------------------------------------------------------------


def schemes_needing_history(
    conn: sqlite3.Connection,
    *,
    category: str | None = None,
    fund_house: str | None = None,
    min_observations: int = config.MIN_OBSERVATIONS,
    limit: int = 50,
) -> list[str]:
    """Catalogued schemes whose stored history is too thin to analyse.

    The catalogue gives every scheme a single NAV per run, so a scheme sitting at
    one or two observations has metadata but no usable history. This is the work
    queue for `backfill_history()`.
    """
    query = """
        SELECT s.scheme_code, COUNT(n.date) AS observations
          FROM scheme_info s
          LEFT JOIN nav_history n ON n.scheme_code = s.scheme_code
         WHERE 1 = 1
    """
    params: list[object] = []
    if category:
        query += " AND s.scheme_category = ?"
        params.append(category)
    if fund_house:
        query += " AND s.fund_house = ?"
        params.append(fund_house)
    # Ordering is the difference between useful coverage in a fortnight and
    # useless coverage in six weeks. Open-ended schemes come first because they
    # are the ones anyone can actually buy: of AMFI's ~14,000 entries, ~4,700 are
    # close-ended, overwhelmingly matured fixed-maturity plans whose history is
    # of archival interest only. Within that, schemes closest to the analysable
    # threshold come first, so each run converts as many funds as it can.
    query += """
         GROUP BY s.scheme_code
        HAVING observations < ?
         ORDER BY CASE
                    WHEN s.scheme_type LIKE 'Open Ended%' THEN 0
                    WHEN s.scheme_type LIKE 'Interval%'   THEN 1
                    ELSE 2
                  END,
                  observations DESC,
                  s.scheme_code
         LIMIT ?
    """
    params.extend([min_observations, limit])
    return [str(row["scheme_code"]) for row in conn.execute(query, params)]


def backfill_history(
    conn: sqlite3.Connection,
    scheme_codes: Sequence[str],
    *,
    polite_delay: float = config.FETCH_POLITE_DELAY_SECONDS,
    time_budget_seconds: float | None = None,
) -> fetch_amfi_data.FetchResult:
    """Fetch full NAV history for specific schemes, within a time budget.

    Full history is one HTTP request per scheme, so fetching all ~14,000 schemes
    would take hours. A scheduled job instead backfills a slice per run and stops
    when the budget expires, letting successive runs complete the universe
    without ever exceeding a job timeout.
    """
    started = time.monotonic()
    result = fetch_amfi_data.FetchResult(requested=list(scheme_codes))
    if not scheme_codes:
        return result

    if not fetch_amfi_data.MFTOOL_AVAILABLE:
        raise fetch_amfi_data.FetchError("mftool is not installed; cannot backfill history")
    if not fetch_amfi_data.amfi_reachable():
        raise fetch_amfi_data.FetchError(f"{config.AMFI_HOST} is not reachable")

    mf = fetch_amfi_data.Mftool()
    run_id = db_manager.start_run(conn, scheme_codes)
    try:
        for index, code in enumerate(scheme_codes):
            if time_budget_seconds and (time.monotonic() - started) > time_budget_seconds:
                logger.info(
                    "Time budget reached after %s/%s schemes; the remainder is left for "
                    "the next run",
                    index,
                    len(scheme_codes),
                )
                break
            try:
                details, records = fetch_amfi_data.fetch_scheme(mf, code)
                if details:
                    db_manager.upsert_scheme_info(
                        conn,
                        str(details.get("scheme_code", code)),
                        str(details.get("scheme_name") or f"Scheme {code}"),
                        details.get("fund_house"),
                        details.get("scheme_type"),
                        details.get("scheme_category"),
                        fetch_amfi_data.SOURCE_AMFI,
                    )
                written, updated = db_manager.upsert_nav_records(
                    conn, records, fetch_amfi_data.SOURCE_AMFI
                )
                result.rows_written += written
                result.rows_updated += updated
                result.succeeded.append(code)
                logger.info("Backfilled %s: %s records (%s new)", code, len(records), written)
            except Exception as exc:
                logger.warning("Backfill failed for %s: %s", code, exc)
                result.failed[code] = str(exc)
            if polite_delay and index < len(scheme_codes) - 1:
                time.sleep(polite_delay)

        db_manager.finish_run(
            conn,
            run_id,
            rows_written=result.rows_written,
            rows_updated=result.rows_updated,
            data_source=fetch_amfi_data.SOURCE_AMFI,
            status="success" if not result.failed else "partial",
            error=str(result.failed) if result.failed else None,
        )
    except Exception as exc:
        db_manager.finish_run(conn, run_id, status="failed", error=str(exc))
        raise
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.catalogue",
        description=(
            "Ingest AMFI's full scheme universe (one request), and optionally backfill "
            "NAV history for a slice of it."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--db-path", default=None, help="Override the SQLite database path")
    parser.add_argument(
        "--no-navs",
        action="store_true",
        help="Store scheme metadata only, skipping the day's NAV snapshot",
    )
    parser.add_argument(
        "--from-file",
        default=None,
        help="Parse a saved NAVAll.txt instead of downloading (offline testing)",
    )
    parser.add_argument(
        "--backfill",
        type=int,
        default=0,
        metavar="N",
        help="After refreshing, fetch full history for up to N schemes that lack it",
    )
    parser.add_argument(
        "--backfill-category",
        default=None,
        help="Restrict the backfill queue to one AMFI category",
    )
    parser.add_argument(
        "--backfill-fund-house", default=None, help="Restrict the backfill queue to one AMC"
    )
    parser.add_argument(
        "--time-budget",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Stop backfilling after this long, leaving the rest for the next run",
    )
    parser.add_argument(
        "--summary-file",
        default=None,
        help="Append a Markdown summary here (point at $GITHUB_STEP_SUMMARY in Actions)",
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    return parser


def _markdown_summary(
    result: CatalogueResult,
    conn: sqlite3.Connection,
    backfill: fetch_amfi_data.FetchResult | None,
) -> str:
    lines = [
        "## AMFI catalogue refresh",
        "",
        f"{result.summary()}",
        "",
        "| Measure | Value |",
        "|---|---:|",
        f"| Schemes in catalogue | {result.parsed:,} |",
        f"| Categories | {result.categories} |",
        f"| Fund houses | {result.fund_houses} |",
        f"| NAV rows written | {result.navs_written:,} |",
        f"| Unparsed lines | {result.skipped_lines} |",
        f"| Duration | {result.duration_seconds:.1f}s |",
        "",
    ]

    rows = list(
        conn.execute(
            """
            SELECT s.scheme_category AS category,
                   COUNT(DISTINCT s.scheme_code) AS schemes,
                   SUM(CASE WHEN h.observations >= ? THEN 1 ELSE 0 END) AS analysable
              FROM scheme_info s
              LEFT JOIN (
                    SELECT scheme_code, COUNT(*) AS observations
                      FROM nav_history GROUP BY scheme_code
                   ) h ON h.scheme_code = s.scheme_code
             WHERE s.scheme_category IS NOT NULL
             GROUP BY s.scheme_category
             ORDER BY schemes DESC
             LIMIT 15
            """,
            (config.MIN_OBSERVATIONS,),
        )
    )
    if rows:
        lines += [
            "### Coverage by category (top 15)",
            "",
            "| Category | Schemes | With analysable history |",
            "|:--|---:|---:|",
        ]
        lines += [
            f"| {row['category']} | {row['schemes']:,} | {row['analysable'] or 0:,} |"
            for row in rows
        ]
        lines.append("")

    if backfill is not None:
        lines += [
            "### History backfill",
            "",
            f"Attempted {len(backfill.requested)} scheme(s): "
            f"{len(backfill.succeeded)} succeeded, {len(backfill.failed)} failed, "
            f"{backfill.rows_written:,} NAV rows added.",
            "",
            "History is one request per scheme, so each run fills a slice; successive "
            "runs complete the universe.",
            "",
        ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    config.ensure_directories()
    conn = db_manager.setup_database(args.db_path)
    try:
        text = (
            Path(args.from_file).read_text(encoding="utf-8", errors="replace")
            if args.from_file
            else None
        )
        result = refresh_catalogue(conn, text=text, store_navs=not args.no_navs)
        if result.errors:
            for error in result.errors:
                logger.error("%s", error)
            return 2

        backfill: fetch_amfi_data.FetchResult | None = None
        if args.backfill:
            queue = schemes_needing_history(
                conn,
                category=args.backfill_category,
                fund_house=args.backfill_fund_house,
                limit=args.backfill,
            )
            logger.info("Backfill queue: %s scheme(s)", len(queue))
            if queue:
                backfill = backfill_history(conn, queue, time_budget_seconds=args.time_budget)
                logger.info("Backfill: %s", backfill.summary())

        print(result.summary())
        if args.summary_file:
            try:
                path = Path(args.summary_file)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(_markdown_summary(result, conn, backfill) + "\n")
            except (OSError, ValueError) as exc:
                logger.warning("Could not write the summary: %s", exc)
        return 0
    except fetch_amfi_data.FetchError as exc:
        logger.error("Catalogue refresh failed: %s", exc)
        return 2
    except Exception:
        logger.exception("Catalogue refresh failed unexpectedly")
        return 1
    finally:
        conn.close()


def iter_categories(conn: sqlite3.Connection) -> Iterable[str]:
    """Distinct AMFI categories present in the catalogue."""
    return (
        str(row["scheme_category"])
        for row in conn.execute(
            "SELECT DISTINCT scheme_category FROM scheme_info "
            "WHERE scheme_category IS NOT NULL ORDER BY scheme_category"
        )
    )


if __name__ == "__main__":
    sys.exit(main())
