# Indian Mutual Fund Tracker & Analytics

[![CI](https://github.com/mchittineni/mutual-fund-analysis-tracker/actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)
[![Pipeline](https://github.com/mchittineni/mutual-fund-analysis-tracker/actions/workflows/pipeline.yml/badge.svg)](../../actions/workflows/pipeline.yml)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/downloads/)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-green.svg)](LICENSE)

**[Live dashboard →](https://mutual-fund-analysis-tracker.streamlit.app/)**

A data pipeline and analytics engine for Indian mutual funds. It pulls daily NAVs from
**AMFI** via `mftool`, stores them in **SQLite** with full provenance, mirrors them to a hosted
**Postgres** database so the dashboard can read them, runs a validation gate, computes a
**performance, risk, and benchmark-relative metric suite**, and publishes the analysis three
ways: a **GitHub Actions job summary**, downloadable **artifacts**, and a self-contained
**GitHub Pages report**. A Streamlit dashboard and a Jupyter notebook read the same engine.

---

## What it computes

| Group | Metrics |
|---|---|
| **Returns** | 3M / 6M absolute, 1Y, 3Y & 5Y CAGR, since-inception CAGR, **SIP XIRR** (monthly cashflow IRR) |
| **Risk** | Annualised volatility, downside deviation, max drawdown (peak/trough/recovery/duration), current drawdown, historical VaR & CVaR (95%), positive-day ratio |
| **Risk-adjusted** | Sharpe, Sortino, Calmar |
| **Benchmark-relative** | CAPM beta, annualised Jensen's alpha, R², tracking error, information ratio, up/down capture, 1Y excess return |
| **Consistency** | Rolling 3-year return distribution — worst / median / best, % positive, % above the risk-free rate |
| **Trend** | 50D & 200D SMAs with a signal that stays `INSUFFICIENT_HISTORY` until the window is genuinely full |
| **Category-relative** | Percentile rank and quartile against the funds in the same AMFI category, direction-corrected so 100 is always good |
| **Composite** | A weighted score whose four components are always shown alongside it, and which is withheld entirely when the peer set is too thin |

Every metric returns `None` rather than a fabricated number when the history is too short, and
the report renders that as `—`, never as `0.00`.

---

## Design principles

These are the rules the code enforces, not aspirations:

1. **Never fabricate financial data.** The synthetic NAV generator is opt-in: `--allow-synthetic`
   permits a fallback when the live fetch fails, `--synthetic-only` forces it and never contacts
   AMFI.
   Every synthetic row is tagged `data_source='synthetic'` in SQLite, flagged by the validator,
   and shouted about at the top of every report. A silent offline fallback that invents returns
   is the single most dangerous thing a tool like this can do.
2. **Validate before you analyse.** Eleven quality checks run first; schemes with critical
   findings are excluded from the metrics and listed in the report. `--fail-on-critical` turns
   this into a CI gate.
3. **Disclose the assumptions.** Risk-free rate, day-count conventions, tax/exit-load treatment,
   and benchmark choice ship with every report, because a Sharpe ratio without its risk-free rate
   is not a number.
4. **One analysis engine.** The pipeline, dashboard, and notebook all call `analyzer.analyse()`,
   so they cannot disagree about the same fund.
5. **Restatements are updates, not conflicts.** AMFI revises NAVs; the database upserts them
   instead of ignoring the second write.
6. **A single number never travels alone.** The composite score always ships its components, and
   it is withheld rather than estimated when fewer than five comparable funds exist — a percentile
   against four peers is noise dressed as precision.
7. **Compare like with like.** Ranking happens inside AMFI's own scheme categories, so the peer set
   is the regulator's definition rather than a judgement call made here.

---

## Architecture

Two ingestion paths, because the two costs are wildly different: the whole universe
arrives in one request, while full price history costs one request *per fund*.

```text
  AMFI NAVAll.txt          AMFI per-scheme history
  (1 request,              (1 request per fund,
   ~14,000 schemes)         full NAV history)
        │                          │
        ▼                          ▼
 ┌──────────────┐          ┌──────────────┐
 │ catalogue.py │          │ fetch_amfi   │ retries + backoff, provenance,
 │              │          │  _data.py    │ audited runs
 │ metadata +   │          └──────┬───────┘
 │ today's NAV  │                 │
 └──────┬───────┘                 │
        └──────────┬──────────────┘
                   ▼
            ┌──────────────┐
            │ db_manager   │ SQLite: UPSERT, WAL, indexes, migrations,
            │              │ ingestion_runs audit trail
            └──────┬───────┘
                   ▼
            ┌──────────────┐
            │ validation   │ 11 checks → CRITICAL / WARNING / INFO
            └──────┬───────┘
                   ▼
   ┌───────────────────────────────┐
   │ metrics.py  (pure functions)  │  returns, risk, benchmark, XIRR
   └───────────────┬───────────────┘
                   ▼
            ┌──────────────┐
            │  peers.py    │ percentile rank within the AMFI category
            └──────┬───────┘
              ┌────┴─────┐
              ▼          ▼
     ┌──────────────┐  ┌──────────────┐
     │ analyzer.py  │  │ screener.py  │ filter + composite score
     │ ranking +    │  │ (components  │
     │ insights     │  │  always shown)│
     └──────┬───────┘  └──────┬───────┘
            ├─────────┬───────┴──────┬──────────────┐
            ▼         ▼              ▼              ▼
      report.md   index.html   report.json     dashboard.py
      (Actions    (Pages)      (machine         (Streamlit)
       summary)                readable)
```

| File | Responsibility |
|---|---|
| [src/config.py](src/config.py) | Paths, universe, benchmark, financial assumptions, thresholds — all env-overridable |
| [src/db_manager.py](src/db_manager.py) | Schema, in-place migrations, upserts, ingestion audit, reads |
| [src/fetch_amfi_data.py](src/fetch_amfi_data.py) | AMFI ingestion with retries; gated synthetic fallback |
| [src/catalogue.py](src/catalogue.py) | Full-universe ingestion from `NAVAll.txt`; budgeted history backfill |
| [src/validation.py](src/validation.py) | Data-quality gate |
| [src/metrics.py](src/metrics.py) | Pure quantitative core (unit-tested against closed-form answers) |
| [src/peers.py](src/peers.py) | Category-relative percentile ranking |
| [src/screener.py](src/screener.py) | Screening and composite scoring, with visible components |
| [src/analyzer.py](src/analyzer.py) | Orchestration, ranking, insight generation |
| [src/report.py](src/report.py) | Markdown / HTML / JSON / CSV rendering, inline SVG charts |
| [main_pipeline.py](main_pipeline.py) | CLI with meaningful exit codes |
| [dashboard.py](dashboard.py) | Streamlit UI over the analysis engine |
| [streamlit_app.py](streamlit_app.py) | Entry point Streamlit Community Cloud auto-detects |
| [src/bootstrap.py](src/bootstrap.py) | First-load data fetch for hosted deployments |
| [src/remote_store.py](src/remote_store.py) | One-way push to hosted Postgres, and the read path back |

---

## Where the data lives

The machine that can reach AMFI and the machine that serves the dashboard are not the
same machine, and neither can be made into the other. GitHub Actions reaches AMFI but its
filesystem is thrown away between runs. Streamlit Community Cloud serves the dashboard but
its filesystem is thrown away too, and its egress may not reach AMFI at all. So a hosted
Postgres database sits between them as the one durable thing both can reach:

```text
        AMFI
          │  (only Actions can reach it)
          ▼
  ┌────────────────┐   COPY + upsert    ┌──────────────┐   SELECT   ┌──────────────┐
  │ GitHub Actions │ ─────────────────▶ │   Postgres   │ ─────────▶ │  Streamlit   │
  │  daily       │  last 5 years,     │  (Supabase)  │            │  any laptop  │
  │ catalogue.py   │  ~3.5M rows,       │              │            │              │
  │ + backfill     │  ~140 seconds      │              │            │              │
  └───────┬────────┘                    └──────────────┘            └──────────────┘
          │
          └──▶ SQLite in an Actions cache (the job's own working copy)
```

SQLite remains the local format: faster, no secrets, and it keeps the test suite offline.
The mirror is a push from it, never a replacement for it.

**Reads choose their source explicitly**, via `MF_STORAGE`, at the `db_manager` boundary —
so `analyzer`, `screener` and the dashboard never learn where a row came from:

| Process | `MF_STORAGE` | Reads from |
|---|---|---|
| Catalogue job (CI) | unset → `sqlite` | its own cached SQLite |
| Hosted dashboard | `supabase` | Postgres |
| Local development | unset → `sqlite` | `data/mf_database.db` |

The switch is deliberately *not* inferred from `MF_REMOTE_URL` being present. The catalogue
job sets that URL in order to **write**; if it also flipped that job's reads, its
before/after progress table would describe the mirror instead of the run it just did.

**Why five years.** Measured against the real store: the full series is 9,374,642 NAV rows
(~712 MB in Postgres) and does not fit Supabase's 500 MB free tier. Five years is
~3.0M rows (~230 MB). Filtering by *scheme* instead was measured and rejected — 9.36M of
those 9.37M rows already belong to analysable schemes, so time is the only lever that
moves the number. Everything the analyzer computes survives the window except
`cagr_since_inception`. Raise `MF_REMOTE_HISTORY_YEARS` on a paid tier.

Verify a mirror at any time — this connects, reports, and writes nothing:

```bash
export MF_REMOTE_URL='postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres'
python -m src.remote_store --check
```

---

## Quick start

```bash
# Python 3.13
pip install -r requirements.txt

python main_pipeline.py                         # fetch, analyse, publish
python -m src.catalogue --backfill 100          # every scheme in India + some history
python -m src.screener --min cagr_3y_pct=12     # screen and score what you have
streamlit run dashboard.py                      # interactive dashboard
```

Reports land in `data/reports/`: `report.md`, `index.html`, `report.json`, `fund_metrics.csv`.

### CLI

```bash
python main_pipeline.py --schemes 119598 125497 120503 --benchmark 120716
python main_pipeline.py --all-analysable         # every fund with enough stored history
python main_pipeline.py --category "Debt Scheme - Liquid Fund"
python main_pipeline.py --fund-house "SBI Mutual Fund" --max-schemes 50
python main_pipeline.py --skip-fetch             # re-analyse stored data, no network
python main_pipeline.py --risk-free-rate 0.072   # move the Sharpe/alpha hurdle
python main_pipeline.py --fail-on-critical       # CI gate on data quality
python main_pipeline.py --allow-synthetic        # offline dev: fall back if AMFI fails
python main_pipeline.py --synthetic-only         # never contact AMFI (tests, CI)
```

| Exit code | Meaning |
|---|---|
| `0` | Success |
| `1` | Unexpected error |
| `2` | Ingestion failed (network / mftool / no data) |
| `3` | Critical data-quality findings with `--fail-on-critical` |

### Covering every mutual fund in India

AMFI publishes its entire universe — roughly 14,000 schemes — in one file,
[`NAVAll.txt`](https://www.amfiindia.com/spages/NAVAll.txt). `mftool` fetches that same file but
keeps only the code and name, **discarding the section headers that carry the scheme type, the
category, and the fund house**. Parsing it directly gets all of that plus both ISINs and the day's
NAV from a single HTTP request.

```bash
python -m src.catalogue                          # every scheme + today's NAV, 1 request
python -m src.catalogue --no-navs                # metadata only
python -m src.catalogue --backfill 200           # + full history for 200 funds that lack it
python -m src.catalogue --backfill 500 --time-budget 1800   # ...and stop after 30 minutes
python -m src.catalogue --backfill-category "Equity Scheme - Small Cap Fund" --backfill 50
python -m src.catalogue --from-file NAVAll.txt   # parse a saved snapshot, no network
```

The two costs are what shape the design:

| | Catalogue | History |
|---|---|---|
| Requests | **1** for all ~14,000 schemes | **1 per scheme** |
| Returns | Metadata, ISINs, today's NAV | Full NAV history |
| Practical cadence | Daily | A slice per run, indefinitely |

So a catalogue run does double duty: it keeps the universe current, and it accumulates one NAV per
scheme per day, growing a real history for every fund over time. `--backfill` fills in the past, and
`--time-budget` stops it cleanly before a runner timeout — successive runs complete the universe
without any single run being long.

**Cataloguing a fund is not the same as being able to analyse it.** A catalogue run gives every
scheme *one* NAV; a fund needs `MF_MIN_OBSERVATIONS` (30) before any metric is computed. So the two
numbers to watch are different:

| | After one catalogue run |
|---|---|
| Schemes catalogued | 14,268 |
| Schemes with **one** NAV | 13,887 |
| Schemes **analysable** | the backfilled ones |

The backfill queue is ordered deliberately. Roughly 4,700 of AMFI's entries are close-ended —
overwhelmingly matured fixed-maturity plans nobody can buy — so open-ended schemes are filled first.
That is the difference between useful coverage in a fortnight and archival coverage in six weeks.
Measured against the live feed, a backfill runs at **~7 seconds per scheme**, so one 45-minute
budget fills roughly 350 funds and the ~8,300 open-ended schemes take about ten days at four runs
a day.

### Screening and scoring

```bash
python -m src.screener --category "Equity Scheme - Large Cap Fund"
python -m src.screener --min cagr_3y_pct=12 --max volatility_pct=18
python -m src.screener --fund-house HDFC --sort-by sharpe_ratio --limit 10
python -m src.screener --explain 119598        # why one fund scores what it does
python -m src.screener --csv build/screen.csv
```

The composite score is the most dangerous thing in this repository — one number invites a decision
while hiding everything that produced it — so three rules constrain it:

1. **The components always travel with the score.** Every table ships `score_returns`,
   `score_risk_adjusted`, `score_drawdown`, and `score_consistency` beside `score`. A 68 built from
   95 on returns and 12 on drawdown is a different fund from a flat 68, and the reader must be able
   to see which one they are looking at.
2. **The inputs are category percentiles, not raw values.** Scoring a liquid fund's 6% against a
   small-cap fund's 24% would rank the entire debt universe last for doing exactly its job. Ranking
   happens *within* AMFI's own categories, and metrics where lower is better (volatility, VaR,
   tracking error) are inverted, so 100 always means good.
3. **A thin peer set produces no score at all.** Below five comparable funds a percentile is noise,
   so the score is absent rather than confident-looking. A missing metric renormalises the remaining
   weights instead of counting as zero — a fund is not penalised for being young.

The weights (returns 35%, risk-adjusted 35%, drawdown 20%, consistency 10%) are editorial, not
derived, and overridable per call. No weighting turns this into advice.

### Configuration

Every setting is env-overridable — no code change needed to retarget an environment:

| Variable | Default | Purpose |
|---|---|---|
| `MF_DB_PATH` | `data/mf_database.db` | SQLite location |
| `MF_REPORT_DIR` | `data/reports` | Report output directory |
| `MF_BENCHMARK_SCHEME` | `120716` | Benchmark scheme code |
| `MF_RISK_FREE_RATE` | `0.065` | Annual risk-free rate (decimal) |
| `MF_SIP_AMOUNT` | `10000` | Monthly SIP instalment for XIRR |
| `MF_MAX_STALENESS_DAYS` | `7` | Staleness warning threshold |
| `MF_EXTREME_MOVE_PCT` | `20` | Single-day move that triggers an outlier warning |
| `MF_MIN_OBSERVATIONS` | `30` | Below this, a scheme is excluded as unanalysable |

Hosted storage — see [Where the data lives](#where-the-data-lives):

| Variable | Default | Purpose |
|---|---|---|
| `MF_STORAGE` | `sqlite` | `supabase` to read from the hosted mirror instead of the local file |
| `MF_REMOTE_URL` | *(unset)* | Postgres URI for the mirror. Unset means the mirror is off entirely |
| `MF_REMOTE_HISTORY_YEARS` | `5` | Years of NAV history to push |
| `MF_REMOTE_BATCH` | `50000` | Rows per COPY batch |

---

## How the analysis is presented (GitHub Actions)

[`.github/workflows/pipeline.yml`](.github/workflows/pipeline.yml) runs every Monday at 02:30 UTC
(08:00 IST) and on manual dispatch. By default it analyses **every fund with enough stored
history** (`--all-analysable`, capped at 250 by `max_schemes`), so the published report grows as the
catalogue job fills coverage in, rather than staying pinned to a hand-picked list. Dispatch with
`universe: configured` for the small list, or set `category` to scope a run. Then it presents the
result at the end:

1. **Job summary** — the pipeline writes its Markdown report straight to `$GITHUB_STEP_SUMMARY`,
   so the full analysis (executive summary, performance, risk, benchmark, rolling returns, data
   quality, assumptions) renders on the run page with nothing to download.
2. **Artifacts** — `report.md`, `index.html`, `report.json`, `fund_metrics.csv`, kept 90 days.
3. **GitHub Pages** — the HTML report, with inline SVG growth and drawdown charts, deployed as a
   browsable site and linked from the run summary.

A failed run writes an exit-code table and a local reproduction command to the summary instead of a
report. The scheduled run never passes `--allow-synthetic`: it publishes real AMFI data or it fails
loudly.

Manual dispatch accepts `schemes`, `benchmark`, `risk_free_rate`, `sip_amount`, and
`fail_on_critical`. All inputs are passed via `env:` and quoted, so they cannot inject shell
commands. The NAV database is cached between runs purely to save time — AMFI returns full history
on every call, so a cache miss costs nothing but minutes.

**One-time setup:** Settings → Pages → Build and deployment → Source: **GitHub Actions**. Until that
is enabled the publish job is skipped, and the summary plus artifacts still work.

### Filling the universe on a schedule

[`.github/workflows/catalogue.yml`](.github/workflows/catalogue.yml) runs daily at 01:00 UTC
(06:30 IST), an hour and a half before the analysis pipeline, and does two things per run:

1. **Refresh the catalogue** — one request, every scheme, metadata plus the day's NAV.
2. **Backfill a slice of history** — stopping at a 45-minute budget so the job ends gracefully with
   its work saved rather than being killed at the runner timeout.
3. **Publish to the hosted database** — the step that makes any of it reachable from the dashboard.
   It runs on `always()`, because a partial catalogue is still worth mirroring, and it is a no-op
   when the `MF_REMOTE_URL` secret is unset. A missing configuration exits 0 rather than failing:
   a red job for an optional feature only teaches people to ignore red jobs.

It runs four times a day rather than once, because the backfill is the bottleneck: the catalogue
only changes when AMFI publishes, but history is one request per scheme.

The job summary shows coverage by category and a before/after table of what the run added, so
progress toward full coverage is visible on every run rather than inferred. The database is carried
between runs through the Actions cache under the same `nav-db-` key prefix the pipeline uses — what
the catalogue fills in, the pipeline analyses — and is saved on `always()`, because a partial
backfill is still progress and discarding it would mean starting over tomorrow.

Manual dispatch accepts `backfill`, `backfill_category`, `backfill_fund_house`, `time_budget`, and
`store_navs`; every input is bound through `env:` and read as a shell variable, so none can be
executed as shell.

> The `NAVAll.txt` parser is covered by tests against a fixture in AMFI's documented format,
> including its awkward cases (a missing NAV, a short line, a duplicate scheme, free-text notices).
> A feed whose format changes parses to zero schemes, which the CLI reports as **exit 2** with a
> pointer at `parse_navall()` rather than silently writing an empty catalogue.

### CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every push and pull request:

| Job | What it does |
|---|---|
| **Quality** | Runs the *same* pre-commit hooks you run locally — ruff lint, ruff format, actionlint, gitleaks, notebook-output stripping, and the guards against committing a database or a generated report. CI and a clean checkout cannot disagree. |
| **Tests** | The full suite on Python 3.13 with coverage, publishing a pass/fail/coverage table to the run summary. Currently **250 tests**, including a headless run of the real Streamlit app. |
| **Smoke** | Runs the real pipeline end to end with `--synthetic-only`, so it never depends on AMFI being reachable, and asserts every artifact exists, that the synthetic data is labelled in all three formats, and that `index.html` is still self-contained (no external assets, no scripts). |

Actions are pinned to commit SHAs, and [Dependabot](.github/dependabot.yml) keeps both the actions
and the Python dependencies current — with pandas and numpy deliberately ungrouped, since they carry
real behavioural risk for the metric layer.

## Development

```bash
pip install -r requirements-dev.txt      # add -r requirements-notebook.txt for the notebook
pre-commit install                       # lint + format on commit
pre-commit install --hook-type pre-push  # tests before push

pytest                      # 125 tests, no network access, ~20s
pytest --cov                # with coverage
ruff check . && ruff format .
pre-commit run --all-files  # everything CI checks
```

[`.pre-commit-config.yaml`](.pre-commit-config.yaml) mirrors CI and adds guards this project needs
specifically: it strips notebook outputs (they leak local paths and real NAV figures into diffs),
lints the workflows with actionlint, scans for secrets, and refuses to commit the SQLite database or
generated reports.

Tests are hermetic: `mftool` is replaced with a fake, each test gets its own SQLite file, and the
quantitative tests assert closed-form answers (a series compounding at exactly 10%/year must
report a 10% CAGR; a doubling over one year must produce an XIRR of exactly 100%; a 2×-leveraged
series must regress to beta 2.0) rather than re-baselining on previous output.

---

## Methodology notes

- **Day-count conventions.** Volatility, Sharpe, Sortino, and tracking error annualise daily
  statistics by √252. CAGR compounds over calendar time using 365.25-day years. XIRR uses a
  365-day year to match Excel and Indian factsheets. Mixing these is deliberate: volatility scales
  with observation count, compound growth with wall-clock time.
- **Sub-one-year returns are never annualised**, matching SEBI/AMFI disclosure convention.
- **Benchmark proxy.** AMFI publishes NAVs, not index levels, so an index *fund* stands in for the
  index. Its NAV is net of a small TER, meaning alpha measured against it is already net of index
  cost.
- **Capture ratios** use monthly returns (industry convention). A negative capture figure means the
  fund moved opposite to the benchmark in those months — a correlation signal, not skill.
- **Not modelled:** taxes, exit loads, expense-ratio changes, dividend/IDCW plans (growth-option
  NAVs only), and survivorship bias in the chosen universe.

---

## Deploying the dashboard

The dashboard runs locally with `streamlit run dashboard.py`. To host it on
[Streamlit Community Cloud](https://share.streamlit.io), point the app at this repository,
branch `main`, main file **`streamlit_app.py`** — then choose how it gets its data.

**The thing to understand first: the filesystem is ephemeral.** Community Cloud wipes the
container on every restart, redeploy, and wake-from-sleep, so any SQLite database written by a
previous run is gone. There are two answers to that, and they behave very differently.

### Reading the hosted mirror (recommended)

Add both of these under **Settings → Secrets**, then reboot:

```toml
MF_REMOTE_URL = "postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres"
MF_STORAGE = "supabase"
```

`MF_STORAGE` is the switch. With the URL alone the app still reads its local SQLite and
nothing changes. Community Cloud exposes secrets as environment variables, so no code change
is needed for them to land.

A cold start becomes a query rather than a download, the app never touches AMFI, and it serves
every scheme the catalogue job has filled in — thousands, not a handful.

Three things that will bite, in the order they usually do:

- **Use the session-pooler URI, port 5432.** Supabase's direct endpoint (`db.<ref>.supabase.co`)
  is IPv6-only and Community Cloud is IPv4, so a direct URI fails as an unexplained connection
  timeout. Port 6543 is transaction-mode pooling, which breaks the prepared statements psycopg
  creates automatically.
- **Row-level security fails silently.** A blocked read returns *zero rows*, not an error, so
  the app renders "0 schemes catalogued" and looks like a data problem. `--check` reports the
  policy picture per table for exactly this reason. A table's owner is exempt from its own RLS
  unless `FORCE ROW LEVEL SECURITY` is set; any other role needs a `SELECT` policy.
- **Free-tier projects pause after 7 days idle.** The scheduled catalogue job's writes keep it
  awake — disable that workflow and the dashboard goes down with it.

### Bootstrapping from AMFI instead

Without `MF_STORAGE`, [`src/bootstrap.py`](src/bootstrap.py) fills an empty database on first
load: it refreshes the full catalogue (every scheme AMFI publishes, one NAV each), then fetches
full history for the seed schemes, caching the result with `st.cache_resource`. A warm container
never re-downloads; the sidebar's **⤓ Fetch NAVs** button forces a refresh.

This keeps the deployment self-contained, but note what it can and cannot give you: the picker
lists the whole universe, while *analysable* funds start at the seed set and grow one NAV day at a
time. It also assumes Community Cloud's egress can reach AMFI, which is not guaranteed.

If the fetch fails, the app says so and offers a retry. It **never falls back to synthetic data** —
a public dashboard silently showing fabricated returns is the worst failure this project could have.

| Variable | Default | Effect on a deployment |
|---|---|---|
| `MF_STORAGE` | `sqlite` | `supabase` reads the hosted mirror; anything else reads the local file |
| `MF_REMOTE_URL` | *(unset)* | Postgres URI. Required when `MF_STORAGE=supabase` |
| `MF_AUTO_BOOTSTRAP` | `1` | Set to `0` when the app must never hit the network |
| `MF_BOOTSTRAP_CATALOGUE` | `1` | Set to `0` to bootstrap only `DEFAULT_TARGET_SCHEMES` |
| `MF_DEFAULT_SELECTION` | `5` | Schemes pre-selected on first paint — a load-time budget, not a view of the universe |
| `MF_CACHE_TTL` | `900` | Seconds before a cached analysis is recomputed |
| `MF_BENCHMARK_SCHEME` | `120716` | Benchmark used for alpha, beta, and capture ratios |
| `MF_RISK_FREE_RATE` | `0.065` | Default position of the risk-free slider |

Set these in the Community Cloud app settings; `config.py` reads its environment at import time, so
they must be present before the process starts.

Three caveats worth knowing before you share the URL:

- **There is no authentication.** Community Cloud apps are public by default. The data is public
  NAV history, but the deployment carries your name.
- **In bootstrap mode, a cold start hits AMFI.** If AMFI is unreachable the app shows an error
  within ~5 seconds (a bounded reachability probe runs before `mftool`'s own unbounded call)
  rather than hanging. Mirror mode never makes that call.
- **In mirror mode, the app holds database credentials.** `MF_REMOTE_URL` grants whatever the
  role in it grants. A dedicated read-only role is the better shape than reusing the owner —
  just create its `SELECT` policies at the same time, or RLS will hand the dashboard an empty
  database.

[`.streamlit/config.toml`](.streamlit/config.toml) holds the theme and server settings. Secrets
never belong there — it is committed.

---

## Project documents

| Document | Purpose |
|---|---|
| [CONTRIBUTING.md](CONTRIBUTING.md) | Setup, the correctness principles, how to add a metric or a data-quality check |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | Contributor Covenant 2.1, plus a no-investment-soliciting rule |
| [SECURITY.md](SECURITY.md) | Private reporting, threat model, existing safeguards, operator hardening |
| [LICENSE](LICENSE) | MIT, with an explicit not-investment-advice notice |

Issue templates cover bugs, feature requests, and — the one that matters most here —
**data-quality reports** for a number that looks wrong.

---

## Disclaimer

This project analyses publicly available AMFI NAV data for research and educational purposes only.
It is not investment advice, and past performance does not predict future returns. Mutual fund
investments are subject to market risk; read all scheme-related documents carefully.
