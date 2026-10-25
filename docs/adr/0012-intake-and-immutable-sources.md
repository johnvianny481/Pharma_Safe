# ADR 0012: Intake channels, idempotency and immutable source storage

- Status: Accepted
- Date: 2026-10-25

## Context

Phase 3 brings reports in through REST, a web/call-centre form, file upload and a monitored mailbox. The build prompt requires:

- idempotency keys on every intake endpoint;
- immutable source storage;
- an OCR interface;
- language detection;
- splitting of files that hold several reports.

Acceptance: the same email processed twice creates one case, and every source document is retrievable byte for byte.

## Decision

**One entry point.** Every channel builds a `Submission` and calls `intake.service.ingest`, which runs in one database transaction.

**Idempotency through a unique submission key.** `intake_submissions` is unique on `(tenant_id, channel, external_id)`:

- API, form and upload channels: `external_id` is the `Idempotency-Key` header, which is required.
- Email: `external_id` is the `Message-ID`, or `sha256:<raw hash>` when there is none.

The insert uses `ON CONFLICT DO NOTHING`, so a concurrent duplicate waits for the first transaction and then returns its result. `request_hash` tells a retry from a reused key:

- the same key and the same content replays the acknowledgment (HTTP 200, `duplicate: true`);
- the same key with different content is refused (HTTP 422).

For email, a different hash under a known `Message-ID` (re-delivery with extra headers) is audited and ignored. No separate idempotency table is needed.

**Email polling is crash-safe.** The poller fetches with `BODY.PEEK[]` and ingests each message in its own transaction. It sets `\Seen` only after the commit. A crash in between re-reads the message, and the unique key turns that into a no-op.

**Immutable sources.**

- Each source document is stored in Azure Blob as `{tenant_id}/{sha256}`: the raw email, its body and attachments, uploads, API text, and the canonical JSON of a form.
- Writes use `If-None-Match: *` and `Content-MD5`, so a blob is never overwritten and corruption in transit is refused.
- Reads re-hash the bytes against the SHA-256 recorded in `source_documents`, and any mismatch is an integrity error.
- The `intake.received` audit entry lists every document's hash, so the tamper-evident ledger covers the sources too.
- Downloads are always `Content-Disposition: attachment` with `nosniff`, so untrusted HTML from an email is never rendered in the browser.

All intake tables are append-only and under row-level security. The application never deletes blobs. In Azure, Terraform (Phase 10) adds:

- a version-level WORM immutability policy on the container;
- an app identity without delete permission;
- a managed identity instead of the shared key.

Azurite emulates none of these, which is why the application enforces them itself.

**Blob client written by hand** (the user's choice over the Azure SDK). `pharmasafe_platform.blobstore.AzureBlobStore` is about 150 lines over `httpx`, with SharedKey signing. It does only three things: create a container, put once, and get with verification. Tests run against Azurite in CI.

**Pre-processing (synchronous for now).**

- *Text extraction:*
  - plain text and CSV, decoded with the declared charset and falling back to Latin-1;
  - HTML, through a stdlib parser;
  - PDF embedded text, through pypdf;
  - images, through `OcrEngine`. `TesseractOcr` calls the `tesseract` CLI when it is installed.

  Scanned PDFs and unknown types are stored and marked `ocr_unavailable` or `unsupported` for manual entry.
- *Language detection:* a deterministic script and function-word scorer for about 15 languages, behind one function. It returns `und` rather than guessing.
- *Report splitting:* rule-based and conservative. A document is split only on two or more numbered report headings on their own lines (in several languages), or on divider lines between substantial blocks. Each report keeps its document id and character span, which later evidence spans refer to.

  If one document in a submission holds several reports, each becomes a case, and the other documents (for example a cover email) stay attached to the submission. Otherwise the submission is one case.

**Cases from intake.**

- The receipt date is set from the server clock (UTC). It is recorded as a system value (source `import`, unconfirmed), so a person must confirm it before approval.
- Form fields are recorded as entered by the intake user.
- Sources are visible to users with unrestricted scope, or to users who can see one of the submission's cases.

## Consequences

- Pre-processing runs inside the request. Phase 4 moves OCR, splitting and case creation into Temporal activities, started from the `intake.received` outbox event.
- An embeddable public form for customer websites is not built. Anonymous submitters cannot be tied to a tenant through a verified token. Possible routes are a per-tenant signed form key, a CAPTCHA and rate limits, or the customer's own backend calling the authenticated API. This needs a decision.
- No automatic reply is sent to email senders. Receipt is acknowledged in the API response and recorded in the audit trail and the outbox. Auto-replies risk mail loops and disclosing personal data.
- The receipt date uses UTC. Tenant time zones for day 0 need a decision with regulatory affairs.
