# Verification record

Local verification on 30 October 2026:

- Python 3.12.6; dependencies captured in `requirements.lock`.
- `pytest`: **25 passed**.
- TypeScript type checking and production Vite build: passed.
- Chromium: upload, explicit six-column mapping, rejected email, identical duplicate, commit, catalog inclusion/exclusion, source report, CSV download and page reload: passed.
- Mobile viewport 390 × 844: no document-level horizontal overflow; tables/navigation scroll within their own containers.
- CLI JSON import with a source-header mapping: two records committed without rejection.
- Desktop and mobile screenshots inspected and saved in `docs/`.

The rollback test injects a SQLite trigger failure on the second destination insert and checks that records, status, revision and commit event remain unchanged. The concurrent replay test submits twelve commits through six threads and checks one destination row and one commit event.

These are bounded correctness checks, not a load test, penetration test or production-service certification. PostgreSQL and distributed-worker behavior are not covered because they are not implemented.
