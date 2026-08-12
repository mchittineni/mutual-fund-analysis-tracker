# Contributing

Thanks for your interest in improving this project. It analyses financial data, so the bar for
correctness is higher than for a typical utility: a wrong number here does not throw an exception,
it quietly misinforms someone about their money. The rules below exist for that reason.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

---

## Non-negotiable principles

Read these before writing code. A PR that violates one will be asked to change regardless of how
well it is implemented.

1. **Never fabricate financial data.** The synthetic NAV generator is opt-in, via
   `--allow-synthetic` (permit a fallback when the live fetch fails) or `--synthetic-only`
   (force it, never contact AMFI). Every synthetic row is tagged
   `data_source='synthetic'` in SQLite, flagged by the validator, and declared at the top of every
   report. Do not add a code path that produces plausible-looking numbers when the real source is
   unavailable, and do not weaken the existing labelling.
2. **Missing is `None`, never `0`.** When history is too short for a metric, return `None` and let
   the report render `—`. A `0.00` reads as a measurement.
3. **Validate before analysing.** New data paths go through [`src/validation.py`](src/validation.py).
   If a new failure mode can silently distort a metric, add a check for it.
4. **Disclose assumptions.** If your change introduces a new financial assumption (a rate, a
   day-count convention, a window), surface it in `config.assumptions()` so it appears in the
   report. An unexplained Sharpe ratio is not a number.
5. **One analysis engine.** The pipeline, dashboard, and notebook all call `analyzer.analyse()`.
   Do not recompute returns in a presentation layer — that is how two views of the same fund start
   disagreeing.
6. **Signals require full windows.** A 50-day average computed from 12 observations is not a
   50-day average. Anything derived from a rolling window must refuse to fire until the window is
   genuinely populated.

---

## Setup

```bash
git clone https://github.com/mchittineni/mutual-fund-analysis-tracker.git
cd mutual-fund-analysis-tracker

python3.13 -m venv .venv          # the project targets Python 3.13
source .venv/bin/activate
pip install -r requirements-dev.txt
pre-commit install                # lint + format on commit
pre-commit install --hook-type pre-push   # tests before push
```

Populate a database without touching AMFI:

```bash
python main_pipeline.py --synthetic-only
```

Mind the difference: `--allow-synthetic` only permits a *fallback* — AMFI is still tried first, so
on a connected machine you get real data. `--synthetic-only` forces generated data and never opens a
socket, which is what tests and CI need.

---

## Development loop

```bash
pytest                      # full suite, no network required
pytest -k xirr              # one area
pytest --cov --cov-report=term-missing
ruff check . && ruff format .
pre-commit run --all-files  # everything CI will check
```

CI runs ruff (lint + format), the test suite with coverage, and a pipeline smoke test that asserts
every report artifact is produced and that synthetic data is labelled. All three must pass.

---

## Repository layout

| Path | What belongs there |
|---|---|
| [`src/config.py`](src/config.py) | Constants, thresholds, financial assumptions. Env-overridable, no side effects on import. |
| [`src/db_manager.py`](src/db_manager.py) | All SQL. Nothing else opens a connection. |
| [`src/fetch_amfi_data.py`](src/fetch_amfi_data.py) | External I/O and provenance tagging. |
| [`src/validation.py`](src/validation.py) | Data-quality checks. No metric maths. |
| [`src/metrics.py`](src/metrics.py) | **Pure functions only** — no I/O, no logging, no database. |
| [`src/analyzer.py`](src/analyzer.py) | Orchestration, ranking, and editorial judgement (named thresholds). |
| [`src/report.py`](src/report.py) | Rendering only. No calculation. |
| [`tests/`](tests/) | One module per source module. |

The separation matters: `metrics.py` stays pure so it can be tested against closed-form answers,
and `analyzer.py` holds the interpretive thresholds so those choices are reviewable rather than
buried in a template.

---

## Adding a metric

1. Write it as a **pure function** in `src/metrics.py`. It takes a series and returns a number or
   `None`. Docstring states the formula, the annualisation convention, and the minimum observation
   count below which it returns `None`.
2. Add a **known-answer test** in `tests/test_metrics.py`. Assert against a closed-form or
   hand-computable result — a series compounding at exactly 10%/year must report 10% CAGR; a
   doubling over 365 days must give an XIRR of exactly 100%; a 2×-leveraged series must regress to
   beta 2.0. Do not assert against whatever the function currently returns; that re-baselines a bug
   into the suite.
3. Add a **None-path test**: too little history must yield `None`, not a number.
4. Add the field to `SchemeMetrics` and to `as_row()`.
5. Surface it in `src/report.py` (Markdown *and* HTML) and, if useful interactively, in
   `dashboard.py`.
6. If it depends on a new assumption, add it to `config.assumptions()`.

## Adding a data-quality check

1. Add it to `_check_scheme()` in `src/validation.py` with a stable snake_case `check` name.
2. Choose severity honestly: **CRITICAL** means "no metric from this scheme can be trusted" and
   excludes it from the report; **WARNING** means "analysable but the reader must know";
   **INFO** is context.
3. Add a test that the check fires on its trigger **and** a test that it stays quiet on clean data.
   A check that fires on everything is noise, and noise gets ignored.
4. If it has a threshold, put the default in `config.py` and accept an override parameter.

---

## Tests

Tests must be **hermetic**: no network, no shared state, no wall-clock dependence beyond fixtures.

- `mftool` is replaced by `FakeMftool` in `tests/test_fetch_and_pipeline.py`. Never let a test
  reach AMFI — CI would then fail for reasons unrelated to the change.
- Each test gets its own SQLite file via the `db_path` fixture.
- Prefer fixtures with analytically known properties (`flat_nav`, `compounding_nav`,
  `drawdown_nav`) over random data.
- Name tests as the behaviour asserted: `test_sma_signal_refuses_to_fire_before_the_window_is_full`,
  not `test_sma_2`.

---

## GitHub Actions

Workflow changes get extra scrutiny for [script injection](https://github.blog/security/vulnerability-research/how-to-catch-github-actions-workflow-injections-before-attackers-do/).

- Never interpolate `${{ ... }}` from event data (issue titles, PR bodies, branch names) directly
  into a `run:` block. Bind it to `env:` and reference it as a quoted shell variable.
- Keep `permissions:` minimal and per-job.
- The scheduled pipeline must keep failing loudly rather than publishing a stale or invalid report.

---

## Pull requests

Keep them focused: one behavioural change, plus its tests and docs. Before opening:

- [ ] `pre-commit run --all-files` passes
- [ ] `pytest` passes
- [ ] New behaviour has a test; a bug fix has a regression test that failed before the fix
- [ ] New financial assumptions appear in `config.assumptions()`
- [ ] Metric changes are reflected in both Markdown and HTML output
- [ ] README updated if you changed the CLI, config, or workflows

Commit messages: imperative mood, explaining *why* rather than restating the diff. A short body
beats a long subject. Conventional-Commits prefixes (`fix:`, `feat:`, `docs:`, `test:`, `refactor:`,
`ci:`) are welcome but not enforced.

**If you change a formula, say so explicitly in the PR description**, including what the number was
before and after on a known input. Silent numerical changes are the hardest class of regression to
catch in review.

---

## Reporting problems

- **Wrong numbers or bad data** → [Data quality issue](https://github.com/mchittineni/mutual-fund-analysis-tracker/issues/new?template=data_quality.yml).
  Include the scheme code, the metric, what the pipeline produced, and what you believe is correct
  with a source.
- **Crashes and defects** → [Bug report](https://github.com/mchittineni/mutual-fund-analysis-tracker/issues/new?template=bug_report.yml).
- **Security** → do not open an issue; follow [SECURITY.md](SECURITY.md).
- **Ideas** → [Feature request](https://github.com/mchittineni/mutual-fund-analysis-tracker/issues/new?template=feature_request.yml),
  or open a discussion first for anything large.

---

## Licence

Contributions are accepted under the [MIT Licence](LICENSE) that covers this project.
