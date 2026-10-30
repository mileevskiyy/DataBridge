# DataBridge roadmap

## Implemented: bounded ingestion workflow

CSV/JSON sources, explicit mapping, canonical ledger rules, duplicate/conflict decisions, preserved validation reports, stale-preview detection, atomic commit, replay safety, catalog provenance, CSV exports, CLI and disposable public demo.

## Next: richer validation contracts

Versioned destination schemas, configurable field rules, explicit locale/date parsing options and reference-data lookups. Acceptance: historical reports remain interpretable after a rule version changes.

## Next: persistent PostgreSQL adapter

Introduce a storage boundary, implement PostgreSQL transactions and compare the same invariant suite across both engines. Acceptance: rollback, concurrency, freshness and replay guarantees pass against a real PostgreSQL service. This adapter is not implemented yet.

## Next: background processing

Persist job state, make claims and retries explicit, add cancellation and record file processing checkpoints. Acceptance: an interrupted worker can resume without duplicating accepted records or losing diagnostics. Add queue infrastructure only when this lifecycle is implemented and tested.

## Next: isolated team workspaces

User authentication, workspace-scoped source/run access, roles and retention controls. Acceptance: cross-workspace API and export access is denied. The shared public demo is not tenant isolation.

## Next: evidence-based performance work

Generate datasets, record hardware and test conditions, measure parser memory/latency and transaction throughput, then optimize the measured bottleneck. No throughput figures are claimed before measurement.
