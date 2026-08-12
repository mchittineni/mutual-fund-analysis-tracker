"""Tests for the persistence layer: schema, migrations, upsert semantics, and auditing."""

from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from src import db_manager


def test_setup_creates_all_tables_and_indexes(conn):
    tables = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"scheme_info", "nav_history", "ingestion_runs", "schema_meta"} <= tables
    indexes = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "idx_nav_scheme_date" in indexes


def test_setup_is_idempotent(db_path):
    db_manager.setup_database(db_path).close()
    conn = db_manager.setup_database(db_path)  # must not raise on an existing database
    version = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    assert int(version[0]) == db_manager.SCHEMA_VERSION
    conn.close()


def test_migration_adds_provenance_columns_to_a_v1_database(db_path):
    """A pre-existing v1 database must gain the new columns in place, not be rebuilt."""
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        "CREATE TABLE scheme_info (scheme_code TEXT PRIMARY KEY, scheme_name TEXT, "
        "fund_house TEXT, scheme_type TEXT, scheme_category TEXT)"
    )
    legacy.execute(
        "CREATE TABLE nav_history (scheme_code TEXT, date DATE, nav REAL, "
        "PRIMARY KEY (scheme_code, date))"
    )
    legacy.execute("INSERT INTO scheme_info VALUES ('111111','Old Fund','AMC','Open','Equity')")
    legacy.execute("INSERT INTO nav_history VALUES ('111111','2024-01-01',100.0)")
    legacy.commit()
    legacy.close()

    conn = db_manager.setup_database(db_path)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(nav_history)")}
    assert {"data_source", "updated_at"} <= columns
    # The legacy row survives, defaulted to unknown provenance.
    row = conn.execute("SELECT nav, data_source FROM nav_history").fetchone()
    assert row["nav"] == 100.0
    assert row["data_source"] == "unknown"
    conn.close()


def test_upsert_nav_inserts_then_reports_no_change_on_a_replay(conn):
    records = [("111111", "2024-01-01", 100.0), ("111111", "2024-01-02", 101.0)]
    inserted, updated = db_manager.upsert_nav_records(conn, records, "amfi")
    assert (inserted, updated) == (2, 0)

    # Replaying identical data must not re-write rows -- updated_at stays meaningful.
    inserted, updated = db_manager.upsert_nav_records(conn, records, "amfi")
    assert inserted == 0
    assert updated == 0


def test_upsert_updates_a_restated_nav(conn):
    """AMFI restates NAVs; INSERT OR IGNORE would keep the wrong value forever."""
    db_manager.upsert_nav_records(conn, [("111111", "2024-01-01", 100.0)], "amfi")
    inserted, updated = db_manager.upsert_nav_records(
        conn, [("111111", "2024-01-01", 102.5)], "amfi"
    )
    assert inserted == 0
    assert updated == 1
    assert conn.execute("SELECT nav FROM nav_history").fetchone()["nav"] == 102.5


def test_upsert_drops_non_positive_navs_without_aborting_the_batch(conn):
    records = [
        ("111111", "2024-01-01", 100.0),
        ("111111", "2024-01-02", 0.0),
        ("111111", "2024-01-03", -5.0),
        ("111111", "2024-01-04", 103.0),
    ]
    inserted, _ = db_manager.upsert_nav_records(conn, records, "amfi")
    assert inserted == 2
    assert conn.execute("SELECT COUNT(*) FROM nav_history").fetchone()[0] == 2


def test_upsert_empty_batch_is_a_no_op(conn):
    assert db_manager.upsert_nav_records(conn, [], "amfi") == (0, 0)


def test_scheme_info_upsert_refreshes_but_never_nulls_existing_metadata(conn):
    db_manager.upsert_scheme_info(conn, "111111", "Fund A", "AMC A", "Open", "Equity", "amfi")
    # A later payload missing fund_house must not erase the value we already have.
    db_manager.upsert_scheme_info(conn, "111111", "Fund A Renamed", None, None, None, "amfi")
    row = conn.execute("SELECT * FROM scheme_info").fetchone()
    assert row["scheme_name"] == "Fund A Renamed"
    assert row["fund_house"] == "AMC A"
    assert row["scheme_category"] == "Equity"


def test_ingestion_run_lifecycle_is_audited(conn):
    run_id = db_manager.start_run(conn, ["111111", "222222"])
    running = conn.execute("SELECT * FROM ingestion_runs WHERE run_id=?", (run_id,)).fetchone()
    assert running["status"] == "running"
    assert running["schemes"] == "111111,222222"

    db_manager.finish_run(conn, run_id, rows_written=10, rows_updated=2, data_source="amfi")
    done = conn.execute("SELECT * FROM ingestion_runs WHERE run_id=?", (run_id,)).fetchone()
    assert done["status"] == "success"
    assert done["rows_written"] == 10
    assert done["finished_at"] is not None


def test_failed_run_records_the_error(conn):
    run_id = db_manager.start_run(conn, ["111111"])
    db_manager.finish_run(conn, run_id, status="failed", error="network down")
    row = conn.execute("SELECT * FROM ingestion_runs WHERE run_id=?", (run_id,)).fetchone()
    assert row["status"] == "failed"
    assert row["error"] == "network down"


def test_load_data_joins_metadata_and_parses_dates(populated_db):
    frame = db_manager.load_data(populated_db)
    assert not frame.empty
    assert set(frame.columns) >= {
        "scheme_code",
        "scheme_name",
        "fund_house",
        "date",
        "nav",
        "data_source",
    }
    assert pd.api.types.is_datetime64_any_dtype(frame["date"])
    # Ordering contract: scheme, then date ascending.
    for _, group in frame.groupby("scheme_code"):
        assert group["date"].is_monotonic_increasing


def test_load_data_filters_by_scheme(populated_db):
    frame = db_manager.load_data(populated_db, scheme_codes=["111111"])
    assert set(frame["scheme_code"].unique()) == {"111111"}


def test_load_data_survives_nav_rows_without_metadata(conn, db_path):
    db_manager.upsert_nav_records(conn, [("999999", "2024-01-01", 50.0)], "amfi")
    conn.close()
    frame = db_manager.load_data(db_path)
    # A LEFT JOIN keeps the orphan visible so validation can flag it.
    assert frame.iloc[0]["scheme_name"] == "Unknown scheme 999999"


def test_scheme_catalogue_reports_coverage(populated_db):
    catalogue = db_manager.scheme_catalogue(populated_db)
    assert len(catalogue) == 2
    assert (catalogue["observations"] > 0).all()
    assert set(catalogue.columns) >= {"first_date", "last_date", "data_source"}


def test_recent_runs_returns_newest_first(conn, db_path):
    for _ in range(3):
        db_manager.finish_run(conn, db_manager.start_run(conn, ["1"]))
    conn.close()
    runs = db_manager.recent_runs(db_path, limit=2)
    assert len(runs) == 2
    assert runs.iloc[0]["run_id"] > runs.iloc[1]["run_id"]


def test_connection_context_manager_commits_and_closes(db_path):
    db_manager.setup_database(db_path).close()
    with db_manager.connection(db_path) as conn:
        db_manager.upsert_nav_records(conn, [("111111", "2024-01-01", 100.0)], "amfi")
    with db_manager.connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM nav_history").fetchone()[0] == 1


def test_nav_check_constraint_rejects_a_direct_bad_insert(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO nav_history (scheme_code, date, nav) VALUES ('1','2024-01-01',-1)"
        )
