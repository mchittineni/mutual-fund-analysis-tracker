"""
Tests for the Postgres mirror.

No Postgres runs here. The behaviour worth pinning down is what the module does
*around* the database: that it stays inert when unconfigured, that it never puts
a password in an error message, and that it selects the right slice of history.
The SQL itself is exercised against a recording fake, which is enough to catch
a column list drifting out of step with the schema.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from src import db_manager, remote_store


def rendered(statement) -> str:
    """The SQL text of a psycopg `Composed`, which does not stringify to itself."""
    return statement.as_string() if hasattr(statement, "as_string") else str(statement)


class FakeCursor:
    """Records statements; answers the two queries the module reads back."""

    def __init__(self, log, fail_on=None):
        self.log = log
        self.fail_on = fail_on
        self._result = None

    def execute(self, statement, params=None):
        text = rendered(statement)
        self.log.append((text, params))
        if self.fail_on and self.fail_on in text:
            raise RuntimeError("connection reset")
        if "RETURNING run_id" in text:
            self._result = (1,)
        return self

    def fetchone(self):
        return self._result

    @property
    def rowcount(self):
        return 0

    def copy(self, statement):
        text = rendered(statement)
        self.log.append((text, "COPY"))
        if self.fail_on and self.fail_on in text:
            raise RuntimeError("connection reset")
        return self

    def write_row(self, row):
        self.log.append(("ROW", row))

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FakeConnection:
    def __init__(self, fail_on=None):
        self.log = []
        self.fail_on = fail_on
        self.commits = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self.log, self.fail_on)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        self.closed = True

    def statements(self):
        return [s for s, _ in self.log]


@pytest.fixture
def local_db(tmp_path):
    """A small SQLite store: one scheme, one recent NAV, one ancient one."""
    path = tmp_path / "local.db"
    conn = db_manager.setup_database(path)
    conn.execute(
        "INSERT INTO scheme_info (scheme_code, scheme_name, fund_house, scheme_category,"
        " data_source) VALUES ('100001', 'A Fund', 'A AMC', 'Equity Scheme - ELSS', 'amfi')"
    )
    recent = (date.today() - timedelta(days=30)).isoformat()
    conn.executemany(
        "INSERT INTO nav_history (scheme_code, date, nav, data_source) VALUES (?, ?, ?, 'amfi')",
        [("100001", recent, 42.0), ("100001", "2001-01-01", 10.0)],
    )
    conn.commit()
    conn.close()
    return path


# --- configuration ---------------------------------------------------------


def test_unconfigured_remote_is_inert(monkeypatch, local_db):
    """The pipeline must run unchanged for anyone who never sets the variable."""
    monkeypatch.delenv(remote_store.REMOTE_URL_ENV, raising=False)
    assert remote_store.remote_configured() is False
    result = remote_store.sync(local_db)
    assert result.skipped is not None
    assert not result.ok
    assert remote_store.REMOTE_URL_ENV in result.summary()


def test_blank_url_counts_as_unset(monkeypatch):
    """A workflow with an unset secret exports an empty string, not nothing."""
    monkeypatch.setenv(remote_store.REMOTE_URL_ENV, "   ")
    assert remote_store.remote_url() is None


def test_connect_without_a_url_is_a_remote_error(monkeypatch):
    monkeypatch.delenv(remote_store.REMOTE_URL_ENV, raising=False)
    with pytest.raises(remote_store.RemoteError, match=remote_store.REMOTE_URL_ENV):
        remote_store.connect()


def test_connection_failure_never_leaks_the_password(monkeypatch):
    """The URI carries a password; an error message is a place it must not go."""
    secret = "postgresql://user:hunter2@db.example.com:5432/postgres"

    class Boom:
        @staticmethod
        def connect(*_args, **_kwargs):
            raise OSError(f"could not translate host name in {secret}")

    monkeypatch.setitem(__import__("sys").modules, "psycopg", Boom)
    with pytest.raises(remote_store.RemoteError) as excinfo:
        remote_store.connect(secret)
    assert "hunter2" not in str(excinfo.value)
    assert "OSError" in str(excinfo.value)


def test_missing_local_database_is_reported_not_raised(monkeypatch, tmp_path):
    monkeypatch.setenv(remote_store.REMOTE_URL_ENV, "postgresql://x/y")
    result = remote_store.sync(tmp_path / "absent.db")
    assert "no local database" in result.skipped


# --- what gets pushed ------------------------------------------------------


def test_sync_pushes_schemes_and_recent_navs(local_db, monkeypatch):
    conn = FakeConnection()
    monkeypatch.setattr(remote_store, "connect", lambda *_a, **_k: conn)
    result = remote_store.sync(local_db, url="postgresql://x/y", history_years=5)

    assert result.ok
    assert result.schemes == 1
    # The 2001 row falls outside the five-year window; the recent one does not.
    assert result.nav_rows == 1
    assert conn.closed

    rows = [payload for statement, payload in conn.log if statement == "ROW"]
    assert ("100001", "A Fund", "A AMC", None, "Equity Scheme - ELSS", None, None, "amfi") in rows
    assert not any("2001-01-01" in str(row) for row in rows)


def test_history_window_is_configurable(local_db, monkeypatch):
    """A paid tier can afford the whole series; the window is the only lever."""
    conn = FakeConnection()
    monkeypatch.setattr(remote_store, "connect", lambda *_a, **_k: conn)
    result = remote_store.sync(local_db, url="postgresql://x/y", history_years=50)
    assert result.nav_rows == 2


def test_sync_creates_the_schema_and_upserts(local_db, monkeypatch):
    """Re-running after a failure must cost time and nothing else."""
    conn = FakeConnection()
    monkeypatch.setattr(remote_store, "connect", lambda *_a, **_k: conn)
    remote_store.sync(local_db, url="postgresql://x/y")

    statements = " ".join(conn.statements())
    assert "CREATE TABLE IF NOT EXISTS nav_history" in statements
    assert "ON CONFLICT" in statements
    assert "DO UPDATE SET" in statements
    # Both natural keys must appear, or a re-run would duplicate rows.
    assert '"scheme_code", "date"' in statements


def test_sync_records_an_audit_row(local_db, monkeypatch):
    conn = FakeConnection()
    monkeypatch.setattr(remote_store, "connect", lambda *_a, **_k: conn)
    remote_store.sync(local_db, url="postgresql://x/y")
    statements = " ".join(conn.statements())
    assert "INSERT INTO sync_runs" in statements
    assert "status = 'ok'" in statements


def test_a_failed_push_is_recorded_then_reraised(local_db, monkeypatch):
    conn = FakeConnection(fail_on="COPY")
    monkeypatch.setattr(remote_store, "connect", lambda *_a, **_k: conn)
    with pytest.raises(RuntimeError, match="connection reset"):
        remote_store.sync(local_db, url="postgresql://x/y")
    assert "status = 'failed'" in " ".join(conn.statements())
    assert conn.closed


def test_the_local_store_is_opened_read_only(local_db, monkeypatch):
    """A mirror must never be able to damage the thing it mirrors."""
    conn = FakeConnection()
    monkeypatch.setattr(remote_store, "connect", lambda *_a, **_k: conn)
    opened = []
    real_connect = sqlite3.connect

    def spy(target, *args, **kwargs):
        opened.append(str(target))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    remote_store.sync(local_db, url="postgresql://x/y")
    assert any("mode=ro" in target for target in opened)


# --- batching --------------------------------------------------------------


def test_rows_are_chunked_into_batches():
    batches = list(remote_store._chunks(iter(range(7)), 3))
    assert [len(b) for b in batches] == [3, 3, 1]


def test_an_empty_source_produces_no_batches():
    assert list(remote_store._chunks(iter([]), 10)) == []


# --- CLI -------------------------------------------------------------------


def test_cli_exits_zero_when_the_mirror_is_unconfigured(monkeypatch, capsys):
    """A red job for an optional feature teaches people to ignore red jobs."""
    monkeypatch.delenv(remote_store.REMOTE_URL_ENV, raising=False)
    assert remote_store.main(["--log-level", "CRITICAL"]) == 0


def test_cli_writes_a_summary_file(monkeypatch, tmp_path, local_db):
    conn = FakeConnection()
    monkeypatch.setattr(remote_store, "connect", lambda *_a, **_k: conn)
    monkeypatch.setenv(remote_store.REMOTE_URL_ENV, "postgresql://x/y")
    summary = tmp_path / "summary.md"
    code = remote_store.main(
        ["--db", str(local_db), "--summary-file", str(summary), "--log-level", "CRITICAL"]
    )
    assert code == 0
    assert "Pushed 1 schemes" in summary.read_text(encoding="utf-8")


# --- connection check ------------------------------------------------------


def test_check_reports_the_server_and_its_tables(monkeypatch):
    """The pre-flight a bad URI should fail on, instead of a 45-minute CI job."""

    class CheckCursor(FakeCursor):
        def __init__(self, log):
            super().__init__(log)
            self.rows = []

        def execute(self, statement, params=None):
            text = rendered(statement)
            self.log.append((text, params))
            if "version()" in text:
                self._result = ("PostgreSQL 17.4 on aarch64", "postgres", "postgres")
            elif "information_schema" in text:
                self.rows = [("nav_history",), ("scheme_info",)]
            elif "FROM nav_history" in text:
                self._result = (3_044_902, "2021-08-14", "2026-08-13")
            elif "FROM scheme_info" in text:
                self._result = (14273,)
            return self

        def fetchall(self):
            return self.rows

    class CheckConnection(FakeConnection):
        def cursor(self):
            return CheckCursor(self.log)

    monkeypatch.setattr(remote_store, "connect", lambda *_a, **_k: CheckConnection())
    report = remote_store.check()
    assert "Connected to postgres as postgres" in report
    assert "PostgreSQL 17.4" in report
    assert "14,273 schemes" in report
    assert "3,044,902 NAV rows" in report


def test_check_exits_two_when_the_url_is_missing(monkeypatch):
    monkeypatch.delenv(remote_store.REMOTE_URL_ENV, raising=False)
    assert remote_store.main(["--check", "--log-level", "CRITICAL"]) == 2
