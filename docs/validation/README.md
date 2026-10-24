# Validation evidence

This folder holds the GAMP 5 validation pack inputs:

- `traceability-matrix.md`: requirements traced to tests (built up from Phase 1).
- Test evidence: CI uploads JUnit XML, coverage, bandit and SBOM files as build artifacts on every run (`python-evidence`, `review-ui-sbom`).
- The AI evaluation report (Phase 8) and the generated per-release validation pack (Phase 10).

## Risk classification

| Risk | Examples | Testing |
|---|---|---|
| High: affects patient safety or regulatory submissions | Seriousness, clocks, E2B generation, signatures, audit ledger | Full scripted testing |
| Lower | UI layout, search, dashboards | Unscripted or automated testing |
