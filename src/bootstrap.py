"""
First-run data bootstrap for hosted deployments.

Streamlit Community Cloud gives every container an **ephemeral filesystem**: the
SQLite database written by a previous run is gone after any restart, redeploy, or
wake-from-sleep. Without this module the hosted dashboard would simply show
"database is empty" to every visitor.

`ensure_database()` fills that gap by running the ingestion step on demand. It is
deliberately conservative:

* It only fetches when the database is empty (or when a caller explicitly forces
  a refresh), so a warm container never re-downloads on every page view.
* It **never falls back to synthetic data** unless a caller explicitly asks. A
  public dashboard silently showing fabricated returns is the worst failure this
  project could have, so a failed fetch surfaces as an error the UI can display.
* It returns a result object rather than raising, because a dashboard should
  render an explanation instead of a stack trace.

The pipeline (`main_pipeline.py`) does not use this module -- it has its own
explicit CLI flags. This exists for environments where no human is present to
run a command first.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from src import config, db_manager, fetch_amfi_data

logger = logging.getLogger(__name__)

Status = Literal["ready", "fetched", "failed", "skipped"]


@dataclass(frozen=True)
class BootstrapResult:
    """What the bootstrap did, in a form the UI can render directly."""

    status: Status
    message: str
    rows_written: int = 0
    schemes: tuple[str, ...] = ()
    synthetic: bool = False
    last_nav_date: date | None = None
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status in {"ready", "fetched"}


def database_state(db_path: str | Path | None = None) -> tuple[int, date | None]:
    """Return ``(nav_row_count, latest_nav_date)`` without creating a schema.

    A missing or unreadable database counts as empty -- callers treat both the
    same way, and a bootstrap check must never be the thing that crashes the app.
    """
    path = Path(db_path) if db_path is not None else config.DB_PATH
    if not path.exists():
        return 0, None
    try:
        with db_manager.connection(path) as conn:
            tables = {
                row["name"]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "nav_history" not in tables:
                return 0, None
            row = conn.execute(
                "SELECT COUNT(*) AS n, MAX(date) AS last FROM nav_history"
            ).fetchone()
            latest = datetime.strptime(row["last"], "%Y-%m-%d").date() if row["last"] else None
            return int(row["n"]), latest
    except Exception as exc:
        logger.warning("Could not inspect %s (%s); treating it as empty", path, exc)
        return 0, None


def ensure_database(
    db_path: str | Path | None = None,
    schemes: Sequence[str] | None = None,
    *,
    benchmark: str | None = config.BENCHMARK_SCHEME,
    force: bool = False,
    allow_synthetic: bool = False,
    enabled: bool = True,
    max_retries: int = 1,
) -> BootstrapResult:
    """Make sure the database holds NAV data, fetching from AMFI if it does not.

    Returns a `BootstrapResult` in every case, including failure. Set ``force``
    to refresh a populated database (the sidebar's refresh button), and
    ``enabled=False`` to disable auto-fetching entirely -- useful for a
    deployment that mounts a pre-built database and should never hit the network.
    """
    started = time.monotonic()
    rows, latest = database_state(db_path)

    if rows and not force:
        return BootstrapResult(
            status="ready",
            message=f"{rows:,} NAV rows already stored (latest {latest}).",
            rows_written=0,
            last_nav_date=latest,
        )

    if not enabled:
        return BootstrapResult(
            status="skipped",
            message=(
                "The database is empty and automatic fetching is disabled "
                "(MF_AUTO_BOOTSTRAP=0). Run `python main_pipeline.py` to populate it."
            ),
        )

    universe = [str(code) for code in (schemes or config.DEFAULT_TARGET_SCHEMES)]
    to_fetch = universe + ([str(benchmark)] if benchmark and str(benchmark) not in universe else [])

    logger.info("Bootstrapping database with %s scheme(s)", len(to_fetch))
    conn = db_manager.setup_database(db_path)
    try:
        result = fetch_amfi_data.fetch_and_store_funds(
            to_fetch,
            conn=conn,
            allow_synthetic=allow_synthetic,
            # One attempt, not three: someone is watching a spinner. The CLI keeps
            # the full retry budget, where waiting is cheaper than failing.
            max_retries=max_retries,
        )
    except fetch_amfi_data.FetchError as exc:
        logger.error("Bootstrap failed: %s", exc)
        return BootstrapResult(
            status="failed",
            message=(
                f"Could not fetch NAV data from AMFI: {exc}. The service may be "
                "temporarily unavailable -- try again in a few minutes."
            ),
            duration_seconds=time.monotonic() - started,
        )
    except Exception as exc:
        logger.exception("Unexpected bootstrap failure")
        return BootstrapResult(
            status="failed",
            message=f"Unexpected error while fetching NAV data: {exc}",
            duration_seconds=time.monotonic() - started,
        )
    finally:
        conn.close()

    _, latest = database_state(db_path)
    return BootstrapResult(
        status="fetched",
        message=(
            f"Fetched {result.rows_written:,} NAV rows for "
            f"{len(result.succeeded)} of {len(to_fetch)} scheme(s)."
            + (f" Failed: {', '.join(result.failed)}." if result.failed else "")
        ),
        rows_written=result.rows_written,
        schemes=tuple(result.succeeded),
        synthetic=result.used_synthetic,
        last_nav_date=latest,
        duration_seconds=time.monotonic() - started,
    )
