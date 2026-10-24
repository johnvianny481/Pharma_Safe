# ADR 0001: Record architecture decisions

- Status: Accepted
- Date: 2026-10-25

## Context

PharmaSafe has to be validated under GxP (GAMP 5, 21 CFR Part 11, EU Annex 11). Validators and auditors need to see why the system is built the way it is, and a decision log is part of that evidence.

## Decision

Each significant decision gets a short ADR in `docs/adr/`. ADRs are numbered in sequence, written in Markdown, and use the sections Context, Decision and Consequences. An ADR is never edited after it is accepted. A later decision replaces it with a new ADR, and the old one is marked "Superseded by NNNN".

## Consequences

- Reviewers can trace any structural choice back to its reasoning.
- Writing an ADR adds a small cost to each significant change. That cost is intended.
