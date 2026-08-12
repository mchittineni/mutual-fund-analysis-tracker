# Security Policy

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Report it privately through GitHub:
[**Open a private security advisory**](https://github.com/mchittineni/mutual-fund-analysis-tracker/security/advisories/new).
Only the maintainer can see it, and it gives us a private space to coordinate a fix and, if
warranted, a CVE.

Please include:

- What the vulnerability is and which file or workflow it lives in
- Reproduction steps or a proof of concept
- The impact you believe it has
- Any suggested fix

**Response expectations.** This is a personal, part-time project, so please calibrate accordingly:
acknowledgement within **7 days**, an initial assessment within **14 days**, and a fix for a
confirmed high-severity issue as promptly as I can manage. If you have not heard back in 14 days,
feel free to ping the advisory thread.

Please give a reasonable window for a fix before disclosing publicly. I am happy to credit you in
the advisory and release notes unless you prefer otherwise.

## Supported versions

Only the tip of the `main` branch is supported. There are no maintained release branches, and fixes
are not backported — update to the latest `main`.

| Version | Supported |
|---|---|
| `main` | Yes |
| Older commits / tags | No |

---

## Threat model

Knowing what this project *is* makes triage faster. It is a batch analytics pipeline that reads
**public** AMFI NAV data, writes a local SQLite file, and renders static reports. It has:

- **No authentication, no user accounts, no sessions**
- **No secrets of its own** — the AMFI endpoint is unauthenticated; the only credential in play is
  the automatic `GITHUB_TOKEN` in Actions
- **No PII and no financial account data** — NAV history is public market data
- **No inbound network listener** — nothing serves traffic; the Streamlit dashboard is intended for
  local use
- **No untrusted-input parsing beyond the AMFI feed** and the CLI arguments you pass it

### In scope

| Area | Concern |
|---|---|
| **GitHub Actions** | Script injection via `${{ }}` interpolation, over-broad `permissions:`, workflow-triggered privilege escalation, cache poisoning between runs |
| **Supply chain** | Malicious or typosquatted dependency, unpinned action resolving to hostile code, dependency confusion |
| **Report rendering** | HTML injection through scheme names or other fetched fields into `index.html`, which is published to GitHub Pages |
| **SQL** | Injection through scheme codes or other parameters into `db_manager` queries |
| **Path handling** | Directory traversal via `--db-path` / `--output-dir` / `MF_*` environment variables in a shared-runner context |
| **Ingestion** | A hostile or compromised upstream response causing code execution, unbounded memory use, or a hang without timeout |
| **Streamlit dashboard** | Anything exploitable if a user chooses to expose it on a network |

### Out of scope

- **Wrong numbers.** An incorrect metric, a bad formula, or a misinterpreted AMFI record is a
  correctness bug, not a vulnerability — please file a
  [data quality issue](https://github.com/mchittineni/mutual-fund-analysis-tracker/issues/new?template=data_quality.yml)
  or a [bug report](https://github.com/mchittineni/mutual-fund-analysis-tracker/issues/new?template=bug_report.yml).
  It matters, it is just handled in public.
- **Financial loss from acting on the output.** The project is research tooling and explicitly not
  investment advice; see [LICENSE](LICENSE).
- AMFI's own infrastructure, availability, or data accuracy. Report those to AMFI.
- Vulnerabilities in third-party dependencies with no exploitable path in this codebase — report
  them upstream, though a heads-up here is welcome.
- Anything requiring an attacker to already have write access to the repository or local shell
  access to your machine.
- Denial of service against your own local run (e.g. passing an enormous scheme list).

---

## Existing safeguards

So you know what has already been considered:

- **Workflow injection.** No workflow interpolates event data (issue titles, PR bodies, branch
  names) into a `run:` block. The only user-controllable values are `workflow_dispatch` inputs,
  which are bound to `env:` and referenced as quoted shell variables. `permissions:` defaults to
  `contents: read`, with `pages: write` / `id-token: write` granted only to the publish job.
- **SQL.** Every query uses parameter binding; no string interpolation of values. The one
  interpolated identifier is a hard-coded table name in a `PRAGMA table_info()` call during
  migration.
- **HTML output.** All fetched text — scheme names, fund houses, quality messages — is passed
  through `html.escape()` before rendering, and a test asserts a `<script>` payload in a scheme name
  is neutralised. The published page contains **no external requests and no `<script>` tags** at all
  (charts are inline SVG), verified by a test, so it survives a strict CSP and has no JS execution
  surface.
- **Ingestion.** Retries are bounded with exponential backoff, per-record parse failures are
  isolated, and non-positive NAVs are rejected at the database boundary by a `CHECK` constraint.
- **Data integrity.** Synthetic data cannot enter a report unlabelled: it requires an explicit
  `--allow-synthetic` flag, is tagged in the database, flagged by the validator, and declared at the
  top of every output. This is enforced by tests, because a pipeline that silently publishes
  fabricated financial figures is the most damaging failure this project could have.
- **Dependency review.** Dependabot keeps dependencies and pinned actions current
  (see [`.github/dependabot.yml`](.github/dependabot.yml)).

## Hardening notes for operators

If you run this yourself:

- Treat `data/mf_database.db` as a cache, not a system of record — it can be rebuilt from AMFI.
- The **Streamlit dashboard has no authentication.** Keep it on `localhost`, or put it behind your
  own authenticating proxy before exposing it.
- Prefer `--fail-on-critical` in any automated context so a degraded dataset stops the run instead
  of being published.
- Never commit real credentials to this repository; nothing here needs any.
