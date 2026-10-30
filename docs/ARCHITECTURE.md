# Architecture and API

## Boundaries

`backend/engine.py` owns bounded parsing, canonicalization, row decisions and SQL state transitions. `backend/app.py` adds HTTP contracts, cookie sessions, origin checks, CSRF and static frontend delivery. `backend/cli.py` uses the same engine directly. React renders the returned source and run contracts rather than calculating authoritative import decisions in the browser.

## Relational model

- `sources`: filename metadata, SHA-256 content digest, ordered headers, raw rows and timestamp. Uploaded bytes are parsed, not executed or stored as user-controlled filesystem paths.
- `runs`: source reference, validation identity, mapping, policy, captured destination revision, immutable row results, status and commit timestamp.
- `records`: canonical external ID key, normalized fields, integer USD cents, most recent run reference and timestamp.
- `metadata`: destination revision used to invalidate stale previews.
- `events`: validation and commit events with run references.

The schema is initialized as version 1 with `PRAGMA user_version`. There is no general migration framework yet.

## Concurrency and failure behavior

One connection and a reentrant lock serialize operations in one application process. A commit uses `BEGIN IMMEDIATE`, checks freshness, applies accepted writes, changes status, increments the destination revision when records changed, appends an event and commits. Exceptions roll back the transaction. Only one Uvicorn worker is supported. Multiple application processes sharing the same file are outside the tested operating model.

Row-level validation errors are expected data outcomes. Storage failures are operation failures: they must not produce a partially promoted run. Accepted rows commit together; rejected rows remain in the report and never reach `records`.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/health` | Liveness and storage mode |
| GET | `/api/session` | Session, public-demo flag and CSRF token |
| POST | `/api/demo-login` | Enter public disposable workspace |
| POST | `/api/login` | Enter private workspace with operator password |
| POST | `/api/logout` | Invalidate current session |
| GET | `/api/workspace` | Run summaries, catalog, revision and activity |
| POST | `/api/sources` | Upload multipart `file` CSV/JSON source |
| POST | `/api/runs/validate` | Validate `{source_id,mapping,policy}` |
| GET | `/api/runs/{id}` | Preserved row decisions and current freshness |
| POST | `/api/runs/{id}/commit` | Transactionally promote accepted records |
| GET | `/api/runs/{id}/errors.csv` | Download rejected source rows and reasons |
| GET | `/api/records.csv` | Download normalized destination data |

All catalog/import/export endpoints require a session. Mutations after login require `X-CSRF-Token`. Login is protected by the origin check and bounded attempts. The HTTP-only session cookie carries a random token; the server retains its SHA-256 digest and expiry. Sessions are process-local even in persistent private mode.

The interactive FastAPI schema is available at `/docs` when the server runs. Mutating operations still need a session and CSRF header; the web interface is the easier walkthrough.

## Resource limits

2 MB file content, 5,000 records, 50 columns, 100-character headers, 2,000-character values, 100 source uploads and 200 validation runs. Workspace source JSON is capped at approximately 10 million characters and report JSON at 20 million characters. JSON serialization uses escaped Unicode, making these conservative content-storage bounds. The request body is also bounded with multipart overhead allowance. No guarantee of unbounded data volume or pagination is made.
