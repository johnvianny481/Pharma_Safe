# ADR 0014: AI gateway, AI pipeline and the signed AI configuration bundle

- Status: Accepted
- Date: 2026-10-25

## Context

Phase 5 adds the AI. The build prompt requires:

- **An AI gateway** with:
  - provider adapters (Azure OpenAI plus one other) and a MockLLM;
  - routing per step, timeouts, token limits, retries with backoff and a fallback provider;
  - per-tenant budgets;
  - redacted logging to Langfuse;
  - pinned model versions.
- **Pipeline steps:**
  - reversible redaction;
  - extraction with evidence spans;
  - dual-signal seriousness;
  - duplicate detection with pgvector;
  - coding by retrieval;
  - follow-up questions;
  - a narrative from confirmed fields only.
- **A signed, versioned AI configuration bundle,** with automation levels L0, L1 and L2 per tenant and serious cases capped at L1.
- **Report text treated as untrusted.**

Acceptance:

- the full pipeline runs on MockLLM in CI;
- an injection test suite passes;
- a provider outage falls back to the second provider, then to L0 manual mode.

The user made four choices:

1. OpenAI direct as the second provider.
2. Provider and Langfuse clients written by hand over httpx, not the vendors' SDKs.
3. Rule-based redaction behind an interface, not Presidio or spaCy.
4. Local, deterministic n-gram vectors for duplicate detection, not an embedding model.

## Decision

### The gateway (`apps/ai_gateway`) is the only path to a model

`POST /v1/steps/{step}` takes:

- the bundle version;
- the case id;
- the step's variables;
- the tenant, in `X-Tenant-Id`, from an internal caller holding the gateway token. Mutual TLS between services replaces the token in Azure (Phase 10).

`GatewayService.run_step` applies every control, in this order:

1. **Bundle.** It loads the signed bundle the caller names and refuses an unknown or untrusted one. The step's prompt and route come from the bundle, not from the caller.
2. **Inputs.** The variables must be exactly the step's declared set, within the route's `max_input_chars`.
3. **Redaction check.** The redaction rules run again on every variable. Detectable personal data means a caller skipped redaction, and the call is refused with HTTP 422. The reply names the kinds of data found, never the values.
4. **Budget.** The month's spend in `ai_calls`, plus this call's worst case (all of `max_tokens`), must fit the tenant's limit (`ai_tenant_budgets`, default USD 500). Otherwise the call is refused with HTTP 429.
5. **Routing.** The primary target gets up to `max_attempts` tries, with exponential backoff and jitter, on retryable errors: timeouts, 408/409/429/5xx, malformed output, and output that fails the schema. Then the fallback target is tried the same way. If both fail, the call is refused with HTTP 503 `providers_unavailable`.
6. **Records.** Every outcome is a row in `ai_calls`, which is append-only and under RLS: ok, fallback_ok, failed, refused_budget or refused_unredacted. The row records bundle and prompt version, provider, model, attempts, tokens, cost and latency. The redacted request and response are logged to Langfuse in the background: one trace per case and one generation per call. Langfuse being down never fails a call.

**Adapters.** The adapters (`AzureOpenAI`, `OpenAI`) use chat completions with **strict structured output** (a JSON schema per step).

- They never send tools or functions.
- They do not retry; retrying happens only in step 5.
- The model named in the response must be the pinned one. OpenAI must report the exact dated snapshot. Azure may also report the base name, because the deployment pins the version on its side. A mismatch is a non-retryable error, so a silent provider upgrade is caught.

**MockLLM.** MockLLM is registered only in dev and test, so a production bundle can never be served by it. The model name selects its behaviour:

| Model | Behaviour |
|---|---|
| `mock-pv-v1` | Answers with schema-valid output from simple rules, with real quotes |
| `mock-outage-v1` | Every call fails as an outage |
| `mock-badjson-v1` | Returns invalid JSON |
| `mock-compromised-v1` | Behaves like a model that has fallen for an injection |

### The bundle is one signed unit (`pharmasafe_platform.aiconfig`)

A bundle is `config/ai_bundles/<version>/bundle.json` (prompts, routes, thresholds, retrieval settings) plus `bundle.sig`.

- **Signature.** The signature is Ed25519 over canonical JSON. Services trust only the public keys in `config/ai_bundles/trusted_keys/`. A tampered file, an unknown signer, a missing signature, or a bundle stored under another version's name is refused.
- **Pinned models.** A model must be a dated snapshot (`gpt-4o-2024-08-06`). Azure targets must also name their deployment and a pinned `api_version`.
- **Committed bundles:**
  - `2026.10.1`: Azure OpenAI primary, OpenAI fallback, not validated yet (Phase 8);
  - `mock-2026.10.1`: MockLLM, used by local development and CI.
- **Change control.** Prompts are generated from `config/ai_bundles/prompts.py`. Changing a bundle means regenerating and re-signing it with the release key, so a bundle change goes through review like code.
- **Keys.** The development key's private half is kept out of the repository (`.secrets/`, git-ignored).
- **Recording.** Every case run records the bundle version it used.

### Redaction (`pharmasafe_domain.ai.redaction`)

**What is replaced.** Identifiers become tokens (`[NAME_1]`, `[EMAIL_1]`, ...):

- emails, phones, URLs;
- labelled ID numbers and dates of birth;
- street addresses;
- names after titles, roles or labels, in English and the main EU languages;
- any personal value already on the case.

**What is kept.** Initials, age, sex and clinical dates stay, because validity and duplicate checks need them.

**Storage.** The mapping (tokens plus a segment map) is stored in `case_redactions`:

- encrypted with AES-256-GCM, using a per-tenant key derived from the master key with HKDF;
- bound by associated data to its case version and document;
- readable only by the cases module.

**Offsets.** The segment map translates evidence found in redacted text back to exact offsets in the original document.

**Limit.** A name with no cue is missed. The gateway's re-check is the second line of defence. A model-based detector can be added behind `find_identifiers` later.

### The pipeline (`apps/ai_worker`) is a child workflow, and its history carries no personal data

The case workflow runs `CasePipeline` (`ai-{case}-v{version}`) on the `ai-pipeline` task queue, guarded by `workflow.patched("ai-pipeline")`. Its activities, in order:

| Activity | What it does |
|---|---|
| `prepare` | Redacts the report and stores the encrypted mapping |
| `extract` | Calls the model to extract fields with evidence |
| `plan` | Checks the extraction and lists the reactions, coding items and gaps the next steps need |
| `assess_seriousness` | Calls the model for the LLM seriousness signal |
| `code_terms` | Calls the model to pick codes from dictionary candidates |
| `ask_follow_up` | Calls the model to word follow-up questions |
| `finalize` | Applies the evidence rule, combines the signals, codes, finds duplicates, sets the automation level, and writes the case and the run |

Temporal stores activity inputs and outputs. Only redacted text, model output quoting that text, ids and flags pass between activities. `finalize` reads the original text and the mapping from the database. A test decodes the whole workflow history and checks that no name, email or phone number from the report is in it.

**The evidence rule** (`proposals.py`) governs every value:

- its quote must be in the redacted text verbatim;
- for free text, numbers and names, the quote must contain the value;
- coded values must be valid for the case model;
- confidence must be within 0 to 1.

A value that fails is rejected with a reason, never repaired. Accepted values become `Tracked` values with `source: ai`, evidence in original document offsets, a confidence, and no confirmation. They only fill empty fields; a value a person entered is never replaced.

**Seriousness has two signals:**

- the LLM, per reaction;
- the rules (`pharmasafe_domain.ai.seriousness`): phrases in five languages with a short negation window.

They are combined with OR, and each disagreement is flagged. Only "met" is ever proposed, with the LLM's evidence or else the rule's. "Not met" is left for a person, because the absence of a mention is not evidence. A run where either signal found seriousness is flagged `serious_by_signals`, and `review_plan_of` then requires quality and medical review even at L0, when nothing was written into the case.

**Coding is retrieval.** A dictionary adapter returns candidates:

- for MedDRA LLTs, for reactions;
- for WHODrug entries, for drugs.

The model may only choose a candidate id. A MedDRA choice fills `meddra_code`, with the reported term's evidence. A WHODrug choice stays a suggestion, because the case model has no WHODrug field yet (Phase 7 maps G.k.2). Development uses `SyntheticDictionary`, with invented codes in a file marked as not MedDRA or WHODrug.

**Duplicates** use pgvector over 256-dimension vectors of hashed character trigrams, built from each case's redacted text and stored in `case_vectors`.

- *Blocking:* candidates must share a suspect drug or reaction term (GIN indexes on arrays).
- *Ranking:* by cosine distance.
- *Scoring:* matching keys plus similarity give a score; the top 3 at or above the threshold are reported with reasons.

RLS keeps the search inside the tenant. Nothing is merged automatically; the merge decision is Phase 6.

**Follow-up gaps come from rules** (`missing_information`). The model only words the questions. A question about a gap it was not given, or one containing personal data, is dropped.

**The narrative** is drafted after the data entry review is signed (`workflow.patched("narrative-draft")`). It uses confirmed fields only, redacted, and is stored as a suggestion (`case_ai_runs`, kind `narrative`). A token the model was not given rejects the draft.

### Automation levels (`pharmasafe_domain.ai.automation`)

- `tenants.ai_automation_level` is L0, L1 (the default) or L2.
- The AI being unavailable means L0 manual mode.
- Serious cases are capped at L1.
- L2 also needs a valid case with every AI value at or above the bundle's threshold.
- L0 writes nothing to the case; the run keeps the proposals for the side panel.
- L1 and L2 write unconfirmed values. L2 is a flag for bulk QC sign-off in Phase 6 and never skips a signature: approval still requires every value confirmed.

### Untrusted text

- Every prompt variable is wrapped in `<data name=… id=NONCE>`. The nonce is random per call, and tag-like text inside is defused.
- The bundle's system message says block content is data.
- No tools are offered.
- Output must match a strict schema.

The injection suite runs ten attacks with a compromised model. It checks that invented values, quotes and codes never reach the case, that seriousness the rules see survives, that questions it was not asked are dropped, and that the case still goes to medical review. A mutation check (evidence checks disabled) makes the suite fail.

### Degradation

- **Gateway.** An outage on the primary falls back to the secondary. If both are down, or the budget is spent, the reply is `providers_unavailable` or `budget_exceeded`.
- **Activity.** The model-call activity then fails with the non-retryable `AiUnavailable`. An unreachable gateway is retried three times first.
- **Pipeline.** The pipeline discards partial output and records the run as `manual_fallback` at L0. The rule seriousness signal and the duplicate search still run.
- **Case workflow.** The case goes on to triage either way, and intake and clocks never wait on the AI.

## Consequences

- **Workflow version.** The case workflow is now version 2. Its v1 history (Phase 4) and a new v2 history both replay in CI.
- **Validation.** The production bundle is not validated. Phase 8 runs it against the labelled set, behind the seriousness-recall release gate. Until then the mock bundle is the default.
- **Redaction recall.** Redaction recall on free text is limited by rules. The gateway check catches only what the same rules catch, so a stronger detector is the next improvement.
- **Pipeline position.** Pre-processing (OCR, splitting) still runs at intake (ADR 0012), and the pipeline starts after it.
- **Follow-up versions.** A follow-up version does not re-run the pipeline yet, because the case workflow resumes without it. The new information's source text is not linked to the follow-up version yet.
- **Budget limits.** Budgets can overshoot by the worst-case cost of calls running in parallel. The limit is a cost control.
- **No prompt caching.** No prompt-prefix caching is done; Phase 9 covers cost tuning.
- **Infrastructure.** `CREATE EXTENSION vector` runs in migration 0005. In Azure, the extension must be allow-listed on the Flexible Server (Phase 10).
