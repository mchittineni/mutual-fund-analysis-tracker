"""
SQLite persistence layer: schema, migrations, upserts, and ingestion auditing.

Design decisions worth knowing:

* **UPSERT, not INSERT OR IGNORE.** AMFI restates NAVs occasionally. `INSERT OR
  IGNORE` would silently keep the first (wrong) value forever, so writes use
  `ON CONFLICT ... DO UPDATE` and only touch the row when the value actually
  changed -- which keeps `updated_at` meaningful.
* **Provenance is a column, not a comment.** `nav_history.data_source` records
  whether a row came from AMFI or from the synthetic generator, so no report can
  ever present fabricated NAVs as real without saying so.
* **Every run is audited.** `ingestion_runs` records what was attempted, what
  landed, and whether it failed, giving the pipeline a queryable history.
* **Idempotent migrations.** `setup_database()` is safe on an existing database
  and adds new columns/indexes in place.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from src import config

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS scheme_info (
        scheme_code      TEXT PRIMARY KEY,
        scheme_name      TEXT NOT NULL,
        fund_house       TEXT,
        scheme_type      TEXT,
        scheme_category  TEXT,
        isin_growth      TEXT,
        isin_reinvest    TEXT,
        data_source      TEXT NOT NULL DEFAULT 'unknown',
        updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS nav_history (
        scheme_code  TEXT NOT NULL,
        date         TEXT NOT NULL,
        nav          REAL NOT NULL CHECK (nav > 0),
        data_source  TEXT NOT NULL DEFAULT 'unknown',
        updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (scheme_code, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ingestion_runs (
        run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at      TEXT NOT NULL,
        finished_at     TEXT,
        schemes         TEXT NOT NULL,
        rows_written    INTEGER NOT NULL DEFAULT 0,
        rows_updated    INTEGER NOT NULL DEFAULT 0,
        data_source     TEXT,
        status          TEXT NOT NULL DEFAULT 'running',
        error           TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_nav_scheme_date ON nav_history (scheme_code, date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_nav_date ON nav_history (date)",
    # The catalogue holds ~14,000 schemes, so category and AMC lookups need indexes.
    "CREATE INDEX IF NOT EXISTS idx_scheme_category ON scheme_info (scheme_category)",
    "CREATE INDEX IF NOT EXISTS idx_scheme_house ON scheme_info (fund_house)",
    "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)


def get_db_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection with the pragmas this workload wants.

    WAL lets the Streamlit dashboard read while the pipeline writes; `foreign_keys`
    and a busy timeout avoid the two classic SQLite footguns.
    """
    path = Path(db_path) if db_path is not None else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def connection(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    """Context-managed connection that always closes, committing on clean exit."""
    conn = get_db_connection(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def setup_database(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Create or migrate the schema and return an open connection.

    Migration from v1 (no provenance columns) happens in place: the columns are
    added with an 'unknown' default rather than requiring a rebuild.
    """
    conn = get_db_connection(db_path)
    for statement in _SCHEMA_STATEMENTS:
        conn.execute(statement)

    # v1 -> v2: provenance and audit columns on pre-existing tables.
    for table, column, ddl in (
        ("scheme_info", "data_source", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("scheme_info", "updated_at", "TEXT"),
        ("nav_history", "data_source", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("nav_history", "updated_at", "TEXT"),
        # v2 -> v3: ISINs from the full-universe catalogue.
        ("scheme_info", "isin_growth", "TEXT"),
        ("scheme_info", "isin_reinvest", "TEXT"),
    ):
        if column not in _existing_columns(conn, table):
            logger.info("Migrating %s: adding column %s", table, column)
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    logger.info("SQLite schema ready at v%s (%s)", SCHEMA_VERSION, config.DB_PATH)
    return conn


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def upsert_scheme_info(
    conn: sqlite3.Connection,
    scheme_code: str,
    scheme_name: str,
    fund_house: str | None,
    scheme_type: str | None,
    scheme_category: str | None,
    data_source: str,
) -> None:
    """Insert or refresh a scheme's metadata."""
    conn.execute(
        """
        INSERT INTO scheme_info
            (scheme_code, scheme_name, fund_house, scheme_type, scheme_category,
             data_source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(scheme_code) DO UPDATE SET
            scheme_name     = excluded.scheme_name,
            fund_house      = COALESCE(excluded.fund_house, scheme_info.fund_house),
            scheme_type     = COALESCE(excluded.scheme_type, scheme_info.scheme_type),
            scheme_category = COALESCE(excluded.scheme_category, scheme_info.scheme_category),
            data_source     = excluded.data_source,
            updated_at      = excluded.updated_at
        """,
        (
            str(scheme_code),
            scheme_name,
            fund_house,
            scheme_type,
            scheme_category,
            data_source,
            _utc_now(),
        ),
    )


def upsert_nav_records(
    conn: sqlite3.Connection,
    records: Sequence[tuple[str, str, float]],
    data_source: str,
) -> tuple[int, int]:
    """Upsert ``(scheme_code, iso_date, nav)`` rows.

    Returns ``(inserted, updated)``. A restated NAV updates the row; an identical
    NAV is left untouched so `updated_at` keeps meaning "when the value changed".
    Non-positive NAVs are dropped here rather than violating the CHECK constraint
    mid-batch and aborting an otherwise good load.
    """
    clean = [
        (str(code), str(day), float(nav))
        for code, day, nav in records
        if nav is not None and float(nav) > 0
    ]
    if not clean:
        return 0, 0

    rows_before, changes_before = _row_count(conn), conn.total_changes
    timestamp = _utc_now()
    conn.executemany(
        """
        INSERT INTO nav_history (scheme_code, date, nav, data_source, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(scheme_code, date) DO UPDATE SET
            nav         = excluded.nav,
            data_source = excluded.data_source,
            updated_at  = excluded.updated_at
        WHERE nav_history.nav != excluded.nav
        """,
        [(code, day, nav, data_source, timestamp) for code, day, nav in clean],
    )
    conn.commit()
    # sqlite3's total_changes counts inserts *and* updates; the row-count delta
    # isolates the inserts, so the difference is the number of restated NAVs.
    # (Timestamp comparison would be wrong here: two calls inside the same second
    # share a timestamp.)
    inserted = _row_count(conn) - rows_before
    updated = (conn.total_changes - changes_before) - inserted
    return inserted, max(updated, 0)


def upsert_catalogue(conn: sqlite3.Connection, entries: Sequence[object]) -> int:
    """Bulk-upsert `catalogue.CatalogueEntry` rows into `scheme_info`.

    Written as one `executemany` because the catalogue is ~14,000 rows: a
    per-row `upsert_scheme_info()` loop would issue 14,000 statements per run.

    Existing values are preserved when the incoming field is NULL, so a later
    per-scheme detail fetch can enrich a catalogue row without a catalogue
    refresh then blanking it again.
    """
    if not entries:
        return 0
    timestamp = _utc_now()
    rows = [
        (
            str(entry.scheme_code),
            entry.scheme_name,
            entry.fund_house,
            entry.scheme_type,
            entry.scheme_category,
            entry.isin_growth,
            entry.isin_reinvest,
            "amfi",
            timestamp,
        )
        for entry in entries
    ]
    before = conn.total_changes
    conn.executemany(
        """
        INSERT INTO scheme_info
            (scheme_code, scheme_name, fund_house, scheme_type, scheme_category,
             isin_growth, isin_reinvest, data_source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(scheme_code) DO UPDATE SET
            scheme_name     = excluded.scheme_name,
            fund_house      = COALESCE(excluded.fund_house, scheme_info.fund_house),
            scheme_type     = COALESCE(excluded.scheme_type, scheme_info.scheme_type),
            scheme_category = COALESCE(excluded.scheme_category, scheme_info.scheme_category),
            isin_growth     = COALESCE(excluded.isin_growth, scheme_info.isin_growth),
            isin_reinvest   = COALESCE(excluded.isin_reinvest, scheme_info.isin_reinvest),
            data_source     = excluded.data_source,
            updated_at      = excluded.updated_at
        """,
        rows,
    )
    conn.commit()
    return conn.total_changes - before


_EMPTY_CATALOGUE_STATS = {
    "schemes": 0,
    "categories": 0,
    "fund_houses": 0,
    "analysable": 0,
    "nav_rows": 0,
}


def catalogue_stats(db_path: str | Path | None = None) -> dict[str, int]:
    """Headline counts for the catalogue, for reports and the dashboard.

    A database that does not exist yet -- the first scheduled run, before any
    cache has been saved -- reports zeros rather than raising. This is a progress
    probe, and a probe must never be the thing that fails the job it measures.
    """
    if _remote_reads():
        from src import remote_store

        return remote_store.catalogue_stats()

    with connection(db_path) as conn:
        tables = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not {"scheme_info", "nav_history"} <= tables:
            return dict(_EMPTY_CATALOGUE_STATS)
        row = conn.execute(
            """
            SELECT COUNT(*) AS schemes,
                   COUNT(DISTINCT scheme_category) AS categories,
                   COUNT(DISTINCT fund_house) AS fund_houses
              FROM scheme_info
            """
        ).fetchone()
        analysable = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT scheme_code FROM nav_history
                 GROUP BY scheme_code HAVING COUNT(*) >= ?
            )
            """,
            (config.MIN_OBSERVATIONS,),
        ).fetchone()[0]
        nav_rows = conn.execute("SELECT COUNT(*) FROM nav_history").fetchone()[0]
    return {
        "schemes": int(row["schemes"]),
        "categories": int(row["categories"]),
        "fund_houses": int(row["fund_houses"]),
        "analysable": int(analysable),
        "nav_rows": int(nav_rows),
    }


def search_schemes(
    db_path: str | Path | None = None,
    *,
    query: str | None = None,
    category: str | None = None,
    fund_house: str | None = None,
    with_history_only: bool = False,
    limit: int = 200,
) -> pd.DataFrame:
    """Search the catalogue. Powers the dashboard's fund browser."""
    if _remote_reads():
        from src import remote_store

        return remote_store.search_schemes(
            query=query,
            category=category,
            fund_house=fund_house,
            with_history_only=with_history_only,
            limit=limit,
        )

    sql = """
        SELECT s.scheme_code, s.scheme_name, s.fund_house, s.scheme_type, s.scheme_category,
               COUNT(n.date) AS observations, MAX(n.date) AS last_date
          FROM scheme_info s
          LEFT JOIN nav_history n ON n.scheme_code = s.scheme_code
         WHERE 1 = 1
    """
    params: list[object] = []
    if query:
        sql += " AND s.scheme_name LIKE ?"
        params.append(f"%{query}%")
    if category:
        sql += " AND s.scheme_category = ?"
        params.append(category)
    if fund_house:
        sql += " AND s.fund_house = ?"
        params.append(fund_house)
    sql += " GROUP BY s.scheme_code"
    if with_history_only:
        sql += " HAVING observations >= ?"
        params.append(config.MIN_OBSERVATIONS)
    sql += " ORDER BY observations DESC, s.scheme_name LIMIT ?"
    params.append(limit)
    with connection(db_path) as conn:
        return pd.read_sql_query(sql, conn, params=params)


def start_run(conn: sqlite3.Connection, schemes: Iterable[str]) -> int:
    """Open an ingestion audit row and return its id."""
    cursor = conn.execute(
        "INSERT INTO ingestion_runs (started_at, schemes) VALUES (?, ?)",
        (_utc_now(), ",".join(str(s) for s in schemes)),
    )
    conn.commit()
    return int(cursor.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    rows_written: int = 0,
    rows_updated: int = 0,
    data_source: str | None = None,
    status: str = "success",
    error: str | None = None,
) -> None:
    """Close an ingestion audit row."""
    conn.execute(
        """
        UPDATE ingestion_runs
           SET finished_at = ?, rows_written = ?, rows_updated = ?, data_source = ?,
               status = ?, error = ?
         WHERE run_id = ?
        """,
        (_utc_now(), rows_written, rows_updated, data_source, status, error, run_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
#
# Each read below can be served either from the local SQLite file or from the
# hosted Postgres mirror, chosen by MF_STORAGE. The branch lives here, at the
# boundary, so that everything upstream -- analyzer, screener, dashboard --
# stays unaware of where a row came from.
#
# Writes are not mirrored: only the ingestion job writes, and it writes SQLite.


def _remote_reads() -> bool:
    """Whether this process should read from the mirror.

    Imported lazily so that a deployment without psycopg, or without the mirror
    configured, never pays for the import.
    """
    from src import remote_store

    return remote_store.reads_enabled()


def load_data(
    db_path: str | Path | None = None, scheme_codes: Sequence[str] | None = None
) -> pd.DataFrame:
    """NAV history joined to scheme metadata, ordered by scheme then date.

    Columns: ``scheme_code, scheme_name, fund_house, scheme_category, date, nav,
    data_source``.
    """
    if _remote_reads():
        from src import remote_store

        return remote_store.load_data(scheme_codes)

    query = """
        SELECT n.scheme_code,
               COALESCE(s.scheme_name, 'Unknown scheme ' || n.scheme_code) AS scheme_name,
               s.fund_house,
               s.scheme_category,
               n.date,
               n.nav,
               n.data_source
          FROM nav_history n
          LEFT JOIN scheme_info s ON n.scheme_code = s.scheme_code
    """
    params: list[str] = []
    if scheme_codes:
        placeholders = ",".join("?" for _ in scheme_codes)
        query += f" WHERE n.scheme_code IN ({placeholders})"
        params = [str(code) for code in scheme_codes]
    query += " ORDER BY n.scheme_code, n.date ASC"

    with connection(db_path) as conn:
        return pd.read_sql_query(query, conn, params=params, parse_dates=["date"])


def scheme_catalogue(db_path: str | Path | None = None) -> pd.DataFrame:
    """One row per scheme with coverage statistics -- the dashboard's index."""
    if _remote_reads():
        from src import remote_store

        return remote_store.scheme_catalogue()

    query = """
        SELECT s.scheme_code, s.scheme_name, s.fund_house, s.scheme_category,
               COUNT(n.date)  AS observations,
               MIN(n.date)    AS first_date,
               MAX(n.date)    AS last_date,
               MAX(n.data_source) AS data_source
          FROM scheme_info s
          LEFT JOIN nav_history n ON n.scheme_code = s.scheme_code
         GROUP BY s.scheme_code
         ORDER BY s.fund_house, s.scheme_name
    """
    with connection(db_path) as conn:
        return pd.read_sql_query(query, conn)


def recent_runs(db_path: str | Path | None = None, limit: int = 10) -> pd.DataFrame:
    """The ingestion audit trail, newest first."""
    with connection(db_path) as conn:
        return pd.read_sql_query(
            "SELECT * FROM ingestion_runs ORDER BY run_id DESC LIMIT ?", conn, params=[limit]
        )


def _row_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM nav_history").fetchone()[0])


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    setup_database().close()
