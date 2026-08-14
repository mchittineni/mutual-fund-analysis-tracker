"""
Mirror the local SQLite store into a hosted Postgres database.

Why this exists: the machine that *can* reach AMFI and the machine that *serves
the dashboard* are not the same machine, and neither can be made into the other.
GitHub Actions reaches AMFI but its filesystem is thrown away; Streamlit
Community Cloud serves the dashboard but its filesystem is thrown away too, and
its egress may not reach AMFI at all. A hosted database is the shared ground
between them:

    Actions (fetches AMFI)  ->  Postgres  ->  Streamlit, and any laptop

SQLite stays the local format. It is faster, needs no secrets, and keeps the
tests offline; this module is a one-way push from it, never a replacement.

Configuration is one environment variable, `MF_REMOTE_URL`, holding a Postgres
URI. Without it every entry point here reports "not configured" and does
nothing, so the pipeline is unchanged for anyone who never sets it.

**Use the pooler URI, not the direct one.** Supabase's direct database endpoint
resolves to IPv6 only; GitHub Actions runners and Streamlit Community Cloud are
IPv4, and a direct URI there fails as a connection timeout with no useful error.
The session-pooler URI (port 5432 on a `pooler.supabase.com` host) is IPv4.

Every identifier below is a module constant. They are still composed through
`psycopg.sql`, which quotes them, so the query text can never be built by
pasting a string in.
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from src import config

logger = logging.getLogger(__name__)

REMOTE_URL_ENV = "MF_REMOTE_URL"

# How much NAV history to mirror. The full series is ~9.4M rows (~700 MB in
# Postgres), which does not fit Supabase's 500 MB free tier; five years is ~3.0M
# rows (~230 MB) and covers every window the analyzer computes except CAGR since
# inception. Raise it if the project moves to a paid tier.
HISTORY_YEARS = float(os.getenv("MF_REMOTE_HISTORY_YEARS", "5"))

# Rows per COPY batch. Large enough that the round trip is amortised, small
# enough that one failure does not cost the whole run.
BATCH_ROWS = int(os.getenv("MF_REMOTE_BATCH", "50000"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS scheme_info (
    scheme_code     TEXT PRIMARY KEY,
    scheme_name     TEXT NOT NULL,
    fund_house      TEXT,
    scheme_type     TEXT,
    scheme_category TEXT,
    isin_growth     TEXT,
    isin_reinvest   TEXT,
    data_source     TEXT NOT NULL DEFAULT 'unknown',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS nav_history (
    scheme_code TEXT NOT NULL,
    date        DATE NOT NULL,
    nav         DOUBLE PRECISION NOT NULL CHECK (nav > 0),
    data_source TEXT NOT NULL DEFAULT 'unknown',
    PRIMARY KEY (scheme_code, date)
);

CREATE INDEX IF NOT EXISTS nav_history_date_idx ON nav_history (date);

CREATE TABLE IF NOT EXISTS sync_runs (
    run_id       BIGSERIAL PRIMARY KEY,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    schemes      INTEGER NOT NULL DEFAULT 0,
    nav_rows     INTEGER NOT NULL DEFAULT 0,
    history_from DATE,
    status       TEXT NOT NULL DEFAULT 'running',
    error        TEXT
);
"""

SCHEME_COLUMNS = (
    "scheme_code",
    "scheme_name",
    "fund_house",
    "scheme_type",
    "scheme_category",
    "isin_growth",
    "isin_reinvest",
    "data_source",
)

NAV_COLUMNS = ("scheme_code", "date", "nav", "data_source")


class RemoteError(RuntimeError):
    """Raised when the remote store is unreachable or misconfigured."""


@dataclass
class SyncResult:
    """What a push moved, in a form the workflow summary can print."""

    schemes: int = 0
    nav_rows: int = 0
    history_from: date | None = None
    duration_seconds: float = 0.0
    skipped: str | None = None

    @property
    def ok(self) -> bool:
        return self.skipped is None

    def summary(self) -> str:
        if self.skipped:
            return f"Remote sync skipped: {self.skipped}"
        return (
            f"Pushed {self.schemes:,} schemes and {self.nav_rows:,} NAV rows"
            + (f" from {self.history_from} onwards" if self.history_from else "")
            + f" in {self.duration_seconds:.0f}s"
        )


def remote_url() -> str | None:
    """The configured Postgres URI, or None when the feature is off."""
    url = os.getenv(REMOTE_URL_ENV, "").strip()
    return url or None


def remote_configured() -> bool:
    return remote_url() is not None


def connect(url: str | None = None):
    """Open a Postgres connection. Raises `RemoteError` rather than leaking driver errors."""
    target = url or remote_url()
    if not target:
        raise RemoteError(f"{REMOTE_URL_ENV} is not set; nothing to connect to")
    try:
        import psycopg
    except ModuleNotFoundError as exc:  # pragma: no cover - import guard
        raise RemoteError(
            "psycopg is not installed. Add `psycopg[binary]` to requirements.txt "
            "to use the remote store."
        ) from exc
    try:
        return psycopg.connect(target, connect_timeout=15)
    except Exception as exc:
        # The URI holds a password. Never let it reach a log line or job summary.
        raise RemoteError(
            f"could not connect to the remote database: {type(exc).__name__}"
        ) from exc


def ensure_schema(conn) -> None:
    """Create the mirror tables. Safe to call on every run."""
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
    conn.commit()


def _cutoff(history_years: float) -> date:
    return date.today() - timedelta(days=round(history_years * 365.25))


def _chunks(rows: Iterator[Sequence], size: int) -> Iterator[list[Sequence]]:
    batch: list[Sequence] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _columns(names: Sequence[str]):
    """`"a", "b", "c"` -- quoted identifiers, never interpolated text."""
    from psycopg import sql

    return sql.SQL(", ").join(sql.Identifier(name) for name in names)


def _copy_into(conn, staging: str, columns: Sequence[str], batch: Sequence[Sequence]) -> None:
    """COPY one batch into a staging table.

    COPY rather than executemany: at three million rows the difference is
    minutes against most of an hour, and the catalogue job has a time budget.
    """
    from psycopg import sql

    statement = (
        sql.SQL("COPY ")
        + sql.Identifier(staging)
        + sql.SQL(" (")
        + _columns(columns)
        + sql.SQL(") FROM STDIN")
    )
    with conn.cursor() as cur, cur.copy(statement) as copy:
        for row in batch:
            copy.write_row(row)


def _merge(conn, staging: str, target: str, columns: Sequence[str], key: Sequence[str]) -> int:
    """Upsert a staging table into its target, then empty the staging table."""
    from psycopg import sql

    assignments = sql.SQL(", ").join(
        sql.Identifier(name) + sql.SQL(" = EXCLUDED.") + sql.Identifier(name)
        for name in columns
        if name not in key
    )
    statement = (
        sql.SQL("INSERT INTO ")
        + sql.Identifier(target)
        + sql.SQL(" (")
        + _columns(columns)
        + sql.SQL(") SELECT ")
        + _columns(columns)
        + sql.SQL(" FROM ")
        + sql.Identifier(staging)
        + sql.SQL(" ON CONFLICT (")
        + _columns(key)
        + sql.SQL(") DO UPDATE SET ")
        + assignments
    )
    with conn.cursor() as cur:
        cur.execute(statement)
        moved = cur.rowcount
        cur.execute(sql.SQL("TRUNCATE ") + sql.Identifier(staging))
    conn.commit()
    return moved


def _create_staging(conn, staging: str, like: str) -> None:
    from psycopg import sql

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("CREATE UNLOGGED TABLE IF NOT EXISTS ")
            + sql.Identifier(staging)
            + sql.SQL(" (LIKE ")
            + sql.Identifier(like)
            + sql.SQL(" INCLUDING DEFAULTS)")
        )
        cur.execute(sql.SQL("TRUNCATE ") + sql.Identifier(staging))
    conn.commit()


def sync(
    sqlite_path: str | Path | None = None,
    *,
    url: str | None = None,
    history_years: float = HISTORY_YEARS,
    batch_rows: int = BATCH_ROWS,
) -> SyncResult:
    """Push the local SQLite store to the remote database.

    One way only, and idempotent: every row is an upsert keyed on its natural
    key, so a re-run after a failure costs time and nothing else.
    """
    started = time.monotonic()
    if not (url or remote_configured()):
        return SyncResult(skipped=f"{REMOTE_URL_ENV} is not set")

    path = Path(sqlite_path) if sqlite_path else config.DB_PATH
    if not path.exists():
        return SyncResult(skipped=f"no local database at {path}")

    cutoff = _cutoff(history_years)
    local = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn = connect(url)
    result = SyncResult(history_from=cutoff)
    run_id = None
    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sync_runs (history_from) VALUES (%s) RETURNING run_id", (cutoff,)
            )
            run_id = cur.fetchone()[0]
        conn.commit()

        _create_staging(conn, "scheme_info_staging", "scheme_info")
        schemes = local.execute(
            "SELECT scheme_code, scheme_name, fund_house, scheme_type, scheme_category,"
            " isin_growth, isin_reinvest, data_source FROM scheme_info"
        )
        for batch in _chunks(schemes, batch_rows):
            _copy_into(conn, "scheme_info_staging", SCHEME_COLUMNS, batch)
            result.schemes += len(batch)
        _merge(conn, "scheme_info_staging", "scheme_info", SCHEME_COLUMNS, ("scheme_code",))
        logger.info("Pushed %s scheme(s)", f"{result.schemes:,}")

        _create_staging(conn, "nav_history_staging", "nav_history")
        navs = local.execute(
            "SELECT scheme_code, date, nav, data_source FROM nav_history WHERE date >= ?",
            (cutoff.isoformat(),),
        )
        for batch in _chunks(navs, batch_rows):
            _copy_into(conn, "nav_history_staging", NAV_COLUMNS, batch)
            result.nav_rows += len(batch)
            # Merge per batch rather than once at the end: the staging table
            # never holds more than one batch, which keeps the tier's disk
            # headroom out of the failure modes.
            _merge(conn, "nav_history_staging", "nav_history", NAV_COLUMNS, ("scheme_code", "date"))
            logger.info("Pushed %s NAV row(s)", f"{result.nav_rows:,}")

        result.duration_seconds = time.monotonic() - started
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sync_runs SET finished_at = now(), schemes = %s, nav_rows = %s,"
                " status = 'ok' WHERE run_id = %s",
                (result.schemes, result.nav_rows, run_id),
            )
        conn.commit()
        return result
    except Exception as exc:
        if run_id is not None:
            try:
                conn.rollback()
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE sync_runs SET finished_at = now(), status = 'failed',"
                        " error = %s WHERE run_id = %s",
                        (f"{type(exc).__name__}: {exc}"[:500], run_id),
                    )
                conn.commit()
            except Exception:  # pragma: no cover - the original error matters more
                logger.debug("Could not record the failed sync run", exc_info=True)
        raise
    finally:
        local.close()
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Push the local SQLite store to the remote Postgres mirror."
    )
    parser.add_argument("--db", default=None, help="SQLite file to read (default: configured path)")
    parser.add_argument(
        "--history-years",
        type=float,
        default=HISTORY_YEARS,
        help="How many years of NAV history to mirror (default: %(default)s)",
    )
    parser.add_argument("--summary-file", default=None, help="Append a markdown summary here")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Connect, report the server and current row counts, then exit without writing",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def check() -> str:
    """Prove the connection string works, without writing anything.

    Worth its own entry point: the alternative way to discover a bad URI is a
    45-minute catalogue job failing on its last step.
    """
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version(), current_database(), current_user")
            version, database, user = cur.fetchone()
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )
            tables = sorted(row[0] for row in cur.fetchall())
        lines = [
            f"Connected to {database} as {user}",
            version.split(" on ")[0],
            f"Tables present: {', '.join(tables) if tables else '(none yet -- the first sync creates them)'}",
        ]
        if "nav_history" in tables:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM nav_history")
                rows, first, last = cur.fetchone()
                cur.execute("SELECT COUNT(*) FROM scheme_info")
                schemes = cur.fetchone()[0]
            lines.append(f"{schemes:,} schemes, {rows:,} NAV rows spanning {first} to {last}")
        return "\n".join(lines)
    finally:
        conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )
    if args.check:
        try:
            logger.info("%s", check())
        except RemoteError as exc:
            logger.error("%s", exc)
            return 2
        return 0
    try:
        result = sync(args.db, history_years=args.history_years)
    except RemoteError as exc:
        logger.error("%s", exc)
        return 2
    logger.info("%s", result.summary())
    if args.summary_file:
        with Path(args.summary_file).open("a", encoding="utf-8") as handle:
            handle.write(f"### Remote sync\n\n{result.summary()}\n\n")
    # A missing configuration is not a failure: the pipeline runs fine without a
    # mirror, and a red job would teach the team to ignore red jobs.
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
