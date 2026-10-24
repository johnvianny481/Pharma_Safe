-- Local development only. Runs once, on first start of an empty Postgres volume.
-- Temporal's auto-setup image creates its own databases (temporal, temporal_visibility).
CREATE DATABASE keycloak;
CREATE DATABASE langfuse;

\connect pharmasafe
-- pgvector for duplicate detection (Phase 5); full-text search is built in.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
