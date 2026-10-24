# ADR 0007: Electronic signatures with OIDC re-authentication

- Status: Accepted
- Date: 2026-10-25

## Context

21 CFR 11.50, 11.70 and 11.200 and EU Annex 11 §14 require several things of an electronic signature:

- it shows the signer, the date and time, and the meaning;
- it is permanently linked to the signed record;
- it needs the signer's credentials at signing time.

PharmaSafe has no passwords of its own because users log in through the customer's IdP.

## Decision

**Re-authentication via the IdP.** To sign, the UI sends the user through a fresh login (`prompt=login`, `max_age=0`) and passes the new access token to the API as proof. `SignatureService` accepts the proof only if all of these hold:

- it has the signer's subject and tenant;
- its `auth_time` is no older than `PHARMASAFE_SIGNATURE_MAX_AUTH_AGE_SECONDS` (default 300 s) and not in the future;
- its `jti` has not been used before. A unique constraint on `e_signatures.reauth_token_id` makes each re-authentication single-use.

The IdP enforces MFA and password policy, so PharmaSafe never handles credentials.

**Meaning and role.** Meanings are a fixed list: Reviewed, Quality reviewed, Medically approved, Approved. Each meaning is allowed for specific roles only, for example *Medically approved* is for `medical_reviewer` only.

**Binding to one record version.** A signature stores the entity type, id, version and the version's content hash, plus a `signature_hash` over all of its own fields. `SignatureService.verify` fails in each of these cases:

- the version differs;
- the content changed;
- the record differs;
- the signature row itself was altered.

**Append-only, with an audit entry.** Signatures are append-only, like the audit ledger, and RLS-isolated. Signing writes a `signature.applied` audit entry in the same transaction.

## Consequences

- The signing API endpoint arrives with case versions in Phase 2. The UI dialog arrives in Phase 6.
- The live test (`apps/api/tests/test_keycloak_realm_live.py`) proves that a real Keycloak re-login yields an accepted proof.
- A user signing a batch must re-authenticate once per signature. Part 11 would allow one full re-authentication per continuous session. That relaxation can be added later as a configurable policy if customers ask for it.
