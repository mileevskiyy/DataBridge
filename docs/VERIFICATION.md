# Verification record

## Versioned schema/recipe upgrade — 1 November 2026

Application commit `86b4b09` passed [GitHub Actions run 41287703485](https://github.com/mileevskiyy/DataBridge/actions/runs/41287703485): 45 Python checks, production build and both browser scenarios. Render deployment `dep-dbjf7orm8hqs73fnhbr0` reached live. Both scenarios passed against `https://databridge-demo.onrender.com/`, including schema/recipe evolution and preserved historical reports, plus the deliberately delayed workspace-refresh regression.

- Python suite: **45 passed**, including the original 25 checks. Existing dependency deprecation warnings remain; they do not fail the suite.
- TypeScript checks and production build passed.
- Browser suite: **2 passed** locally. The original ledger workflow remains covered. The new scenario creates a schema through the editor, imports a semicolon/comma-decimal/DMY file, saves a recipe, commits valid data into its own catalog, publishes tighter rules and a new recipe version, then reopens the original unchanged report. Desktop/mobile screenshots were inspected; no document-level horizontal overflow at 390 px.
- New engine/API checks cover exact schema/recipe pins, historical replay identity, stale editors, stable dataset shapes, independent destination revisions, custom upsert/replayed commits, missing headers/delimiter mismatch, audit-failure rollback, persistent restart, additive V1-column migration, malformed rules, extreme numeric bounds, version limits and spreadsheet-safe export headers.
- CLI test launches a separate Python process, loads a saved recipe, commits and reopens the resulting custom dataset.
- Public follow-up exposed a delayed-refresh UI race: remounting the catalog after a commit could clear a search already entered by the user. The catalog now refetches without discarding its selected schema/search state. The browser scenario deliberately delays the workspace response until after catalog navigation/search and verifies that both survive.
- No PostgreSQL adapter, external reference-data integration, background processing or measured throughput is claimed.

The original record below is historical.


Local verification on 31 October 2026:

- Python 3.12.6; dependencies captured in `requirements.lock`.
- `pytest`: **25 passed**.
- TypeScript type checking and production Vite build: passed.
- Chromium: upload, explicit six-column mapping, rejected email, identical duplicate, commit, catalog inclusion/exclusion, source report, CSV download and page reload: passed.
- Mobile viewport 390 × 844: no document-level horizontal overflow; tables/navigation scroll within their own containers.
- CLI JSON import with a source-header mapping: two records committed without rejection.
- Desktop and mobile screenshots inspected and saved in `docs/`.

The rollback test injects a SQLite trigger failure on the second destination insert and checks that records, status, revision and commit event remain unchanged. The concurrent replay test submits twelve commits through six threads and checks one destination row and one commit event.

These are bounded correctness checks, not a load test, penetration test or production-service certification. PostgreSQL and distributed-worker behavior are not covered because they are not implemented.
