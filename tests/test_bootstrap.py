"""
Tests for the hosted-deployment bootstrap.

The behaviour that matters most here is what happens when AMFI is unreachable: a
public dashboard must show an error, never fabricated returns and never a stack
trace.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src import bootstrap, config, db_manager, fetch_amfi_data
from tests.test_fetch_and_pipeline import FakeMftool


@pytest.fixture
def fake_amfi(monkeypatch):
    def install(instance):
        monkeypatch.setattr(fetch_amfi_data, "MFTOOL_AVAILABLE", True)
        monkeypatch.setattr(fetch_amfi_data, "amfi_reachable", lambda *_a, **_k: True)
        monkeypatch.setattr(fetch_amfi_data, "Mftool", lambda *_a, **_k: instance)
        monkeypatch.setattr(fetch_amfi_data.config, "FETCH_POLITE_DELAY_SECONDS", 0)
        return instance

    return install


# --- database_state --------------------------------------------------------


def test_database_state_reports_empty_for_a_missing_file(tmp_path):
    rows, latest = bootstrap.database_state(tmp_path / "absent.db")
    assert (rows, latest) == (0, None)


def test_database_state_reports_empty_for_a_schema_without_rows(db_path):
    db_manager.setup_database(db_path).close()
    assert bootstrap.database_state(db_path) == (0, None)


def test_database_state_counts_rows_and_finds_the_latest_date(populated_db):
    rows, latest = bootstrap.database_state(populated_db)
    assert rows > 0
    assert latest is not None


def test_database_state_treats_a_corrupt_file_as_empty(tmp_path):
    """A damaged database must not be the thing that crashes the app."""
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is definitely not sqlite")
    assert bootstrap.database_state(corrupt) == (0, None)


# --- ensure_database -------------------------------------------------------


def test_populated_database_is_left_alone(populated_db, monkeypatch):
    """A warm container must not re-download on every page view."""

    def explode(*_args, **_kwargs):
        raise AssertionError("ensure_database fetched despite existing data")

    monkeypatch.setattr(fetch_amfi_data, "fetch_and_store_funds", explode)
    result = bootstrap.ensure_database(populated_db)
    assert result.status == "ready"
    assert result.ok
    assert result.rows_written == 0


def test_empty_database_is_populated_from_amfi(db_path, fake_amfi):
    fake_amfi(FakeMftool())
    result = bootstrap.ensure_database(db_path, schemes=["111111"], benchmark=None)
    assert result.status == "fetched"
    assert result.ok
    assert result.rows_written > 0
    assert not result.synthetic
    assert result.last_nav_date is not None


def test_bootstrap_includes_the_benchmark(db_path, fake_amfi):
    fake_amfi(FakeMftool())
    result = bootstrap.ensure_database(db_path, schemes=["111111"], benchmark="120716")
    assert set(result.schemes) == {"111111", "120716"}


def test_force_refreshes_a_populated_database(populated_db, fake_amfi):
    fake_amfi(FakeMftool())
    result = bootstrap.ensure_database(populated_db, schemes=["111111"], benchmark=None, force=True)
    assert result.status == "fetched"


def test_disabled_bootstrap_reports_rather_than_fetching(db_path, monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("ensure_database fetched while disabled")

    monkeypatch.setattr(fetch_amfi_data, "fetch_and_store_funds", explode)
    result = bootstrap.ensure_database(db_path, enabled=False)
    assert result.status == "skipped"
    assert not result.ok
    assert "MF_AUTO_BOOTSTRAP" in result.message


def test_fetch_failure_returns_an_error_not_an_exception(db_path, monkeypatch):
    """The hosted app renders this message; it must never see a traceback."""
    monkeypatch.setattr(fetch_amfi_data, "MFTOOL_AVAILABLE", False)
    result = bootstrap.ensure_database(db_path, schemes=["111111"], benchmark=None)
    assert result.status == "failed"
    assert not result.ok
    assert "AMFI" in result.message


def test_bootstrap_never_fabricates_data_by_default(db_path, monkeypatch):
    """The critical property for a public deployment: a failed fetch leaves the
    database empty rather than filling it with generated returns."""
    monkeypatch.setattr(fetch_amfi_data, "MFTOOL_AVAILABLE", False)
    result = bootstrap.ensure_database(db_path, schemes=["111111"], benchmark=None)
    assert not result.synthetic
    assert bootstrap.database_state(db_path)[0] == 0


def test_synthetic_is_available_when_explicitly_requested(db_path, monkeypatch):
    monkeypatch.setattr(fetch_amfi_data, "MFTOOL_AVAILABLE", False)
    result = bootstrap.ensure_database(
        db_path, schemes=["119598"], benchmark=None, allow_synthetic=True
    )
    assert result.status == "fetched"
    assert result.synthetic is True


def test_unexpected_errors_are_caught_and_described(db_path, monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(fetch_amfi_data, "fetch_and_store_funds", explode)
    result = bootstrap.ensure_database(db_path, schemes=["111111"], benchmark=None)
    assert result.status == "failed"
    assert "disk on fire" in result.message


def test_partial_failure_is_reported_but_still_usable(db_path, fake_amfi, monkeypatch):
    monkeypatch.setattr(fetch_amfi_data.config, "FETCH_MAX_RETRIES", 1)
    fake_amfi(FakeMftool(fail_codes={"222222"}))
    result = bootstrap.ensure_database(db_path, schemes=["111111", "222222"], benchmark=None)
    assert result.status == "fetched"
    assert result.ok
    assert "Failed: 222222" in result.message


def test_result_message_is_renderable(db_path, fake_amfi):
    """Every status must carry a message the UI can display verbatim."""
    fake_amfi(FakeMftool())
    for result in (
        bootstrap.ensure_database(db_path, schemes=["111111"], benchmark=None),
        bootstrap.ensure_database(db_path, schemes=["111111"], benchmark=None),
    ):
        assert isinstance(result.message, str) and result.message.strip()


# --- entry point -----------------------------------------------------------


def test_streamlit_entry_point_reexecutes_rather_than_importing():
    """Regression test for a blank app after the first interaction.

    Streamlit re-runs the entry script on every widget change, but `import` hits
    `sys.modules` and returns the cached module without re-running its body. An
    entry point built on `import dashboard` therefore renders once and then goes
    blank. `runpy.run_path` re-executes the file every time.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent / "streamlit_app.py"
    # Inspect executable statements only: the docstring names the anti-pattern on
    # purpose, and matching prose is how this assertion would lie.
    code = [
        line.strip()
        for line in source.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    body = "\n".join(code)
    assert "runpy.run_path" in body
    assert not any(line.startswith("import dashboard") for line in code)
    assert not any(line.startswith("from dashboard import") for line in code)


def test_dashboard_survives_widget_reruns(tmp_path, monkeypatch):
    """Render the real app, then drive it -- the failure this catches produces an
    empty page with no exception, which unit tests cannot see."""
    pytest.importorskip("streamlit")
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    from src import fetch_amfi_data

    db = tmp_path / "app.db"
    conn = db_manager.setup_database(db)
    fetch_amfi_data.generate_synthetic_history(conn, ["119598", "120716"], years=4.0)
    conn.close()

    # config reads its environment at import time, and the module is already
    # imported by now, so patch the attributes the app actually consults.
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(config, "AUTO_BOOTSTRAP", False)

    entry = Path(__file__).resolve().parent.parent / "streamlit_app.py"
    app = AppTest.from_file(str(entry), default_timeout=120)
    app.run()
    assert not app.exception
    # Assert the labels, not a count: a count is a tripwire that fires whenever a
    # tab is added, and says nothing about whether the page actually rendered.
    tabs = {tab.label for tab in app.tabs}
    assert {"Overview", "Risk", "Screener", "Scheme detail"} <= tabs, (
        "the app did not render on first load"
    )
    assert app.metric, "no metrics rendered"

    # The regression: a second run must still render a full page.
    app.sidebar.number_input[0].set_value(9.0).run()
    assert not app.exception
    assert {tab.label for tab in app.tabs} == tabs, "the app went blank after a widget interaction"
    assert app.metric


def test_dashboard_renders_the_assumptions_table(tmp_path, monkeypatch):
    """Arrow cannot serialise a column mixing floats and strings, which is exactly
    what config.assumptions() is; rendering it raw broke the Data quality tab."""
    pytest.importorskip("streamlit")
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    from src import fetch_amfi_data

    db = tmp_path / "app.db"
    conn = db_manager.setup_database(db)
    fetch_amfi_data.generate_synthetic_history(conn, ["119598"], years=4.0)
    conn.close()

    # config reads its environment at import time, and the module is already
    # imported by now, so patch the attributes the app actually consults.
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(config, "AUTO_BOOTSTRAP", False)

    entry = Path(__file__).resolve().parent.parent / "streamlit_app.py"
    app = AppTest.from_file(str(entry), default_timeout=120)
    app.run()
    assert not app.exception
    rendered = [df.value for df in app.dataframe]
    assumptions = [d for d in rendered if list(d.columns) == ["Assumption", "Value"]]
    assert assumptions, "the assumptions table was not rendered"
    assert assumptions[0]["Value"].map(type).eq(str).all(), "mixed types will break Arrow"


def test_streamlit_config_is_valid_toml_and_holds_no_secrets():
    import tomllib
    from pathlib import Path

    parsed = tomllib.loads(Path(".streamlit/config.toml").read_text())
    assert parsed["server"]["headless"] is True

    # Check the declared settings, not the prose: a comment saying "secrets never
    # belong here" is exactly right and must not fail the test.
    def leaf_keys(mapping, prefix=""):
        for key, value in mapping.items():
            if isinstance(value, dict):
                yield from leaf_keys(value, f"{prefix}{key}.")
            else:
                yield f"{prefix}{key}".lower(), str(value).lower()

    for key, value in leaf_keys(parsed):
        assert not any(word in key for word in ("password", "token", "secret", "api_key"))
        assert not value.startswith(("sk-", "gh", "ghp_"))


def test_runtime_requirements_exclude_notebook_dependencies():
    """Hosted cold starts install requirements.txt; jupyter must not be in it."""
    from pathlib import Path

    # Only the requirement lines count; the file's comments explain the split.
    declared = {
        line.split(">=")[0].split("==")[0].strip().lower()
        for line in Path("requirements.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "-"))
    }
    assert "streamlit" in declared
    assert "plotly" in declared
    assert not declared & {"jupyter", "matplotlib", "ipykernel", "notebook"}

    notebook = Path("requirements-notebook.txt").read_text().lower()
    assert "jupyter" in notebook and "matplotlib" in notebook
