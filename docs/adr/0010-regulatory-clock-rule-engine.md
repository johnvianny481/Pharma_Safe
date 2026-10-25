# ADR 0010: Regulatory clock rule engine

- Status: Accepted
- Date: 2026-10-25

## Context

Day 0 is the date the company first had all four minimum criteria. Due dates come from versioned, configurable rule sets per regulator (FDA, EMA, CDSCO), seeded with example rules that are clearly marked as configurable. Clocks must survive crashes and must never be reset.

## Decision

**Rule sets are data.** Each rule set is a JSON document (`pharmasafe_domain/rules/rule_sets/*.json`) validated by `ClockRuleSet`. It has:

- a regulator, a version and an `effective_from` date;
- `example: true` and a notice;
- the regulator's domestic countries;
- rules.

Each rule matches on:

- setting: postmarketing, study or any;
- seriousness: serious, non-serious or any;
- `unexpected_only`;
- `fatal_or_life_threatening_only`;
- occurrence: domestic, foreign or any.

It gives `due_in_days` in calendar days. The rule set in force on the clock-start date applies, and an already-started clock keeps its rule set version.

**Seeded rules are examples.** Their versions end in `-example` and their notice says they are not regulatory advice. A test asserts this. Regulatory affairs must review and configure real rule sets.

**Matching is per reaction.** "Serious and unexpected" means one reaction is both, not one serious reaction and another unexpected one. When the case does not say enough to decide, the engine assumes the case is reportable, because a missed deadline costs more than an unnecessary report. This applies when:

- the report type is unknown (matches both settings);
- the country is unknown (matches domestic and foreign);
- expectedness is not assessed (counts as unexpected).

**Clocks start once and never move.** `ClockTracker.observe` evaluates the current state of a case version. For each applicable rule without a clock yet for that version, it starts one on the state's receipt date (C.1.5). Effects:

- A follow-up that adds seriousness starts a new clock from the follow-up's receipt date.
- Existing clocks are untouched.
- Day 0 is the receipt date of the first valid state, and it is set once.

The database enforces both:

- `cases.day_zero` cannot change once set (trigger);
- `case_clocks` is append-only.

`evaluate_history` (batch) and the incremental tracker must agree. Property tests check this together with:

- due date equals start plus the rule period;
- no clock starts before day 0 or from an invalid state;
- clocks from a prefix of history survive unchanged.

**Tenant regulators.** `tenants.regulators` (default `{FDA,EMA}`) lists the regulators whose rules apply.

## Consequences

- For now, rule sets ship with the code, so changing them goes through the normal release and validation process. Per-tenant rule configuration (stored, signed and versioned in the database) belongs with the tenant admin screens (Phase 6).
- Every follow-up that matches a rule starts a clock. Deciding that a follow-up is not significant, and waiving its report, is a signed human task in the Phase 4 workflow.
- A wrong receipt date corrected after a clock started does not move the clock. Correcting a clock needs a documented, audited administrative action, which is not built yet.
- Escalation before deadlines is Phase 4 (Temporal timers read `case_clocks`).
