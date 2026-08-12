<!--
Thanks for contributing. Keep the PR focused on one behavioural change.
See CONTRIBUTING.md for the principles this project holds itself to.
-->

## What and why

<!-- What does this change, and what problem does it solve? Link the issue: Closes #123 -->

## Type of change

- [ ] Bug fix
- [ ] New metric or analysis capability
- [ ] Data-quality check
- [ ] Ingestion / database
- [ ] Reporting or dashboard
- [ ] CI / tooling / docs
- [ ] Breaking change (CLI flags, config names, database schema, or JSON report shape)

## Does this change any number the project reports?

<!--
Answer honestly — a silent numerical change is the hardest regression to catch in review.
If yes, show before/after on a known input and say which formula or convention moved.
-->

- [ ] No — no computed value changes
- [ ] Yes — details below

```
Metric:
Input (scheme / fixture):
Before:
After:
Why the new value is correct:
```

## Verification

- [ ] `pytest` passes locally
- [ ] `pre-commit run --all-files` passes
- [ ] New behaviour has a test; a bug fix has a regression test that **failed before** the fix
- [ ] Ran the pipeline end to end (`python main_pipeline.py --allow-synthetic`) and checked the
      rendered report

## Correctness checklist

<!-- Tick what applies; delete lines that clearly do not. -->

- [ ] Insufficient data returns `None`, never `0` or a fabricated value
- [ ] No new code path can produce synthetic/placeholder data without the `--allow-synthetic` gate
      and its provenance label
- [ ] New rolling-window logic refuses to emit a signal until the window is genuinely full
- [ ] New financial assumptions are surfaced in `config.assumptions()` so they reach the report
- [ ] `src/metrics.py` additions are pure functions (no I/O, no logging, no database)
- [ ] Metric changes are reflected in **both** the Markdown and the HTML report
- [ ] No return calculation was duplicated in a presentation layer

## Documentation

- [ ] README updated (CLI, config, workflows, or metric table)
- [ ] Docstrings state formulas, annualisation conventions, and minimum observation counts

## Workflow changes only

- [ ] No `${{ }}` event data is interpolated into a `run:` block (bound via `env:` and quoted instead)
- [ ] `permissions:` remain minimal and scoped per job

## Notes for the reviewer

<!-- Anything you are unsure about, deliberately left out, or want pushback on. -->
