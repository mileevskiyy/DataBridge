# Portfolio wording

## CV

**DataBridge — Data Quality & Ingestion Platform** | Python, FastAPI, SQL, SQLite, React, TypeScript

- Built a staged CSV/JSON ingestion workflow with configurable column mapping, deterministic row validation, duplicate reconciliation and downloadable error reports.
- Implemented transactionally consistent promotion, replay-safe commits and destination-revision checks that prevent stale validation results from overwriting updated records.
- Preserved source-to-record provenance and historical validation snapshots; verified failure rollback, concurrent retries and the complete browser workflow.

## LinkedIn description

DataBridge is a full-stack data ingestion application for converting imperfect CSV and JSON feeds into a canonical customer ledger. The Python backend separates file parsing, validation and promotion; the React interface exposes mapping decisions, rejected rows, import history and destination-record provenance.

The engineering focus is correctness under imperfect input and repeated requests: explicit conflict policies, exact decimal-to-cent conversion, atomic commits, stable validation identities, idempotent commit replay and revision-based detection of stale previews. A shared web/CLI engine keeps the rules consistent across interactive and scripted workflows.

The repository includes automated domain/API checks, a complete Chromium import scenario, Docker packaging and a disposable public demonstration. Current scope is a bounded, single-process SQLite application; PostgreSQL, background workers and multi-tenant workspaces are future extensions.

Suggested skills: Python · SQL · FastAPI · Data Validation · ETL · SQLite · REST APIs · React · TypeScript · Automated Testing · GitHub Actions · Docker.
