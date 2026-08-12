#!/usr/bin/env python3
"""
Orchestration CLI for the Indian Mutual Fund tracker.

    python main_pipeline.py                        # fetch, analyse, report
    python main_pipeline.py --skip-fetch           # re-analyse what is already stored
    python main_pipeline.py --schemes 119598 120503 --benchmark 120716
    python main_pipeline.py --allow-synthetic      # offline development (fallback only)
    python main_pipeline.py --synthetic-only       # never touch AMFI (tests, CI)
    python main_pipeline.py --fail-on-critical     # CI gate: exit non-zero on bad data

Exit codes are meaningful so CI can react to *why* a run failed:

* ``0`` success
* ``1`` unexpected error
* ``2`` ingestion failed (network / mftool / no data)
* ``3`` critical data-quality findings and ``--fail-on-critical`` was set
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from src import analyzer, config, db_manager, fetch_amfi_data, report

EXIT_OK, EXIT_ERROR, EXIT_FETCH_FAILED, EXIT_QUALITY_FAILED = 0, 1, 2, 3

logger = logging.getLogger("pipeline")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch AMFI NAV data, analyse performance and risk, and publish a report.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--schemes",
        nargs="+",
        metavar="CODE",
        default=None,
        help="AMFI scheme codes to analyse (default: the configured universe)",
    )
    parser.add_argument(
        "--all-analysable",
        action="store_true",
        help="Analyse every scheme in the database with enough stored NAV history, "
        "instead of the configured universe. Use after `python -m src.catalogue` has "
        "filled the database; implies --skip-fetch, since re-fetching thousands of "
        "schemes one at a time would take hours",
    )
    parser.add_argument(
        "--category",
        default=None,
        help="Restrict the universe to one AMFI category, e.g. "
        "'Equity Scheme - Large Cap Fund'. Implies --all-analysable",
    )
    parser.add_argument(
        "--fund-house",
        default=None,
        help="Restrict the universe to one AMC. Implies --all-analysable",
    )
    parser.add_argument(
        "--max-schemes",
        type=int,
        default=100,
        metavar="N",
        help="Cap on the size of a database-derived universe. Ranked by history depth, "
        "so the best-covered funds come first",
    )
    parser.add_argument(
        "--benchmark",
        default=config.BENCHMARK_SCHEME,
        help="AMFI scheme code used as the benchmark; 'none' disables relative metrics",
    )
    parser.add_argument(
        "--risk-free-rate",
        type=float,
        default=config.RISK_FREE_RATE,
        help="Annual risk-free rate as a decimal (0.065 = 6.5%%) for Sharpe/Sortino/alpha",
    )
    parser.add_argument(
        "--sip-amount",
        type=float,
        default=config.SIP_MONTHLY_AMOUNT,
        help="Monthly SIP instalment in INR used for the XIRR calculation",
    )
    parser.add_argument("--db-path", default=None, help="Override the SQLite database path")
    parser.add_argument(
        "--output-dir", default=None, help="Directory for report.md / index.html / report.json"
    )
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Analyse the stored data without contacting AMFI",
    )
    parser.add_argument(
        "--allow-synthetic",
        action="store_true",
        help="Permit generated NAV data when the live fetch FAILS (development only). "
        "AMFI is still tried first, so a machine with network access produces real "
        "data; every synthetic row is labelled in the database and the report",
    )
    parser.add_argument(
        "--synthetic-only",
        action="store_true",
        help="Force generated NAV data and never contact AMFI. Use for hermetic tests "
        "and CI, where --allow-synthetic alone would silently produce real data",
    )
    parser.add_argument(
        "--fail-on-critical",
        action="store_true",
        help="Exit 3 when the data-quality gate reports a critical finding",
    )
    parser.add_argument(
        "--summary-file",
        default=os.getenv("GITHUB_STEP_SUMMARY"),
        help="Append the Markdown report here (defaults to $GITHUB_STEP_SUMMARY in Actions)",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("MF_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    return parser


def resolve_universe(args: argparse.Namespace) -> tuple[list[str], bool]:
    """Decide which schemes to analyse, and whether ingestion should be skipped.

    Three sources, in order of precedence:

    1. ``--schemes`` -- an explicit list.
    2. ``--all-analysable`` / ``--category`` / ``--fund-house`` -- everything in
       the database with enough stored history. This is what turns a filled
       catalogue into an analysis: the catalogue knows about ~14,000 schemes, but
       only the ones with real NAV history can be measured.
    3. The configured universe -- a small hand-picked list.

    A database-derived universe implies ``--skip-fetch``. Fetching full history is
    one request per scheme, so re-fetching a few hundred would take longer than
    the analysis itself -- and the data is already stored, which is the entire
    reason those schemes qualified.
    """
    if args.schemes:
        return [str(code) for code in args.schemes], args.skip_fetch

    from_database = args.all_analysable or bool(args.category) or bool(args.fund_house)
    if not from_database:
        return [str(code) for code in config.DEFAULT_TARGET_SCHEMES], args.skip_fetch

    found = db_manager.search_schemes(
        args.db_path,
        category=args.category,
        fund_house=args.fund_house,
        with_history_only=True,
        limit=max(1, args.max_schemes),
    )
    if found.empty:
        scope = (
            f" in {args.category or args.fund_house}" if (args.category or args.fund_house) else ""
        )
        if args.category or args.fund_house:
            # An explicit filter that matches nothing is a mistake worth stopping
            # for -- silently analysing something else would answer a question
            # the caller did not ask.
            logger.error(
                "No scheme%s has at least %s stored NAV observations. Run "
                "`python -m src.catalogue --backfill 500 --backfill-category %r` first.",
                scope,
                config.MIN_OBSERVATIONS,
                args.category or args.fund_house,
            )
            return [], True

        # A bare --all-analysable against an empty database is the scheduled-job
        # case: the Actions cache was evicted, or this is a first run. Falling
        # back to the configured universe keeps the report publishing instead of
        # failing outright -- but loudly, because a three-fund report where a
        # few-hundred-fund one was expected must not pass unnoticed.
        logger.warning(
            "No scheme in the database has at least %s stored NAV observations, so "
            "--all-analysable has nothing to analyse. Falling back to the configured "
            "universe of %s scheme(s) and fetching it. Run "
            "`python -m src.catalogue --backfill 500` to build coverage.",
            config.MIN_OBSERVATIONS,
            len(config.DEFAULT_TARGET_SCHEMES),
        )
        return [str(code) for code in config.DEFAULT_TARGET_SCHEMES], args.skip_fetch

    codes = [str(code) for code in found["scheme_code"]]
    logger.info(
        "Universe from the database: %s scheme(s) with >= %s observations%s",
        len(codes),
        config.MIN_OBSERVATIONS,
        " (capped by --max-schemes)" if len(codes) >= args.max_schemes else "",
    )
    return codes, True


def run_pipeline(args: argparse.Namespace) -> int:
    """Execute fetch -> validate -> analyse -> report and return a process exit code."""
    config.ensure_directories()
    schemes, skip_fetch = resolve_universe(args)
    if not schemes:
        return EXIT_ERROR
    benchmark = None if str(args.benchmark).lower() in {"none", ""} else str(args.benchmark)

    # A universe of 200 codes would make this line unreadable, so summarise past a
    # handful and let --log-level DEBUG show the rest.
    shown = ", ".join(schemes) if len(schemes) <= 8 else f"{len(schemes)} schemes"
    logger.info("Universe: %s | benchmark: %s", shown, benchmark or "disabled")
    logger.debug("Full universe: %s", ", ".join(schemes))

    # --- Step 1: ingest -------------------------------------------------
    if skip_fetch:
        logger.info("[1/3] Skipping ingestion (--skip-fetch)")
        db_manager.setup_database(args.db_path).close()
    else:
        # The benchmark needs its own NAV history to compare against.
        to_fetch = schemes + ([benchmark] if benchmark and benchmark not in schemes else [])
        logger.info("[1/3] Fetching %s scheme(s) from AMFI", len(to_fetch))
        conn = db_manager.setup_database(args.db_path)
        try:
            result = fetch_amfi_data.fetch_and_store_funds(
                to_fetch,
                conn=conn,
                # --synthetic-only implies permission; asking for both would be noise.
                allow_synthetic=args.allow_synthetic or args.synthetic_only,
                synthetic_only=args.synthetic_only,
            )
            logger.info("Ingestion: %s", result.summary())
        except fetch_amfi_data.FetchError as exc:
            logger.error("Ingestion failed: %s", exc)
            logger.error(
                "Re-run with --skip-fetch to analyse stored data, or --synthetic-only to "
                "generate clearly-labelled test data."
            )
            return EXIT_FETCH_FAILED
        finally:
            conn.close()

    # --- Step 2: validate + analyse -------------------------------------
    logger.info("[2/3] Validating data and computing performance & risk metrics")
    analysis = analyzer.analyse(
        args.db_path,
        scheme_codes=schemes,
        benchmark_code=benchmark,
        risk_free_rate=args.risk_free_rate,
        sip_amount=args.sip_amount,
    )

    # --- Step 3: publish -------------------------------------------------
    logger.info("[3/3] Rendering reports")
    paths = report.write_reports(analysis, args.output_dir)
    print(report.render_console(analysis))
    for label, path in paths.items():
        logger.info("  %-8s %s", label, path)

    if args.summary_file:
        _append_summary(Path(args.summary_file), report.render_markdown(analysis))

    if analysis.quality.has_critical:
        for finding in analysis.quality.critical:
            logger.error("Data quality: %s", finding)
        if args.fail_on_critical:
            logger.error("Failing the run: %s critical finding(s)", len(analysis.quality.critical))
            return EXIT_QUALITY_FAILED

    if not analysis.schemes:
        logger.error("No scheme produced metrics; nothing was published")
        return EXIT_QUALITY_FAILED if args.fail_on_critical else EXIT_ERROR

    logger.info("Pipeline complete: %s scheme(s) analysed", len(analysis.schemes))
    return EXIT_OK


def _append_summary(path: Path, markdown: str) -> None:
    """Append the Markdown report to a summary file (GitHub Actions job summary).

    A failure to write the summary must never fail the analysis, so this only warns.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")
        logger.info("Markdown summary appended to %s", path)
    except (OSError, ValueError) as exc:
        logger.warning("Could not write summary to %s: %s", path, exc)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return run_pipeline(args)
    except KeyboardInterrupt:
        logger.warning("Interrupted")
        return EXIT_ERROR
    except Exception:
        logger.exception("Pipeline failed with an unexpected error")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
