# Requirements traceability matrix

Requirements come from the build prompt and the Part 11 / Annex 11 controls in the product architecture. Each one is traced to the automated tests that provide evidence. CI stores the test results as JUnit XML (`python-evidence` artifact).

Risk: **H** means it affects patient safety or regulatory submissions and gets full scripted testing. **L** means lower risk.

## Phase 1: identity, tenancy, audit, e-signatures

| ID | Requirement | Risk | Evidence (tests) |
|---|---|---|---|
| IAM-01 | API accepts only tokens signed by the configured IdP, for the right audience and issuer, and not expired | H | `packages/platform/tests/test_auth.py`: `test_rejects_invalid_tokens`, `test_rejects_token_signed_by_another_key`, `test_rejects_unsigned_token` |
| IAM-02 | Roles come from the token; unknown roles are ignored | H | `test_auth.py`: `test_valid_token_yields_principal`, `test_unknown_roles_are_ignored` |
| IAM-03 | Data scopes by product and region; missing scope grants nothing | H | `test_auth.py::TestDataScope` |
| IAM-04 | Endpoints enforce roles; platform admins cannot read tenant data | H | `apps/api/tests/test_api_auth.py`: `test_audit_entries_need_an_audit_role`, `test_platform_admin_cannot_read_tenant_data`, `test_quality_reviewer_cannot_run_verification` |
| IAM-05 | The local IdP realm issues the claims the API relies on | L | `apps/api/tests/test_keycloak_realm_live.py` (local stack only) |
| TEN-01 | Tenant comes only from the verified token, never from request parameters | H | `test_api_auth.py::test_audit_entries_are_isolated_by_token_tenant` |
| TEN-02 | A tenant cannot read another tenant's rows | H | `packages/platform/tests/test_tenant_isolation_db.py`: `test_tenant_sees_only_its_own_rows`, `test_unknown_tenant_sees_nothing`; `test_signatures_db.py::test_signatures_are_tenant_isolated` |
| TEN-03 | A tenant cannot write rows into another tenant | H | `test_tenant_isolation_db.py::test_tenant_cannot_write_into_another_tenant` |
| TEN-04 | No tenant context means no tenant data; context does not leak across pooled connections | H | `test_tenant_isolation_db.py`: `test_session_without_tenant_sees_nothing`, `test_tenant_context_does_not_leak_across_pooled_transactions` |
| TEN-05 | Every tenant table has forced RLS; the app role cannot bypass RLS | H | `test_tenant_isolation_db.py`: `test_every_tenant_table_has_forced_rls`, `test_app_role_cannot_bypass_rls`, `test_app_role_cannot_create_tenants` |
| AUD-01 | Audit entries record who, what, when, old and new value, reason and case version, hash-chained per tenant | H | `test_audit_ledger_db.py`: `test_entries_form_a_chain`, `test_chains_are_independent_per_tenant`; `test_audit_hashing.py` |
| AUD-02 | The application role cannot update, delete or truncate audit entries | H | `test_audit_ledger_db.py::test_app_role_cannot_modify_ledger` |
| AUD-03 | Not even the table owner can modify the audit trail | H | `test_audit_ledger_db.py::test_even_owner_cannot_modify_ledger` |
| AUD-04 | Chain verification detects edited, re-hashed and deleted entries | H | `test_audit_ledger_db.py`: `test_detects_edited_entry`, `test_detects_edit_with_recomputed_hash`, `test_detects_deleted_entry`; `apps/api/tests/test_verify_job.py` |
| AUD-05 | Chain stays valid under concurrent writers and for any JSON value | H | `test_audit_ledger_db.py`: `test_concurrent_appends_keep_the_chain_intact`, `test_json_values_survive_jsonb_normalisation` |
| AUD-06 | A change and its audit entry commit or roll back together | H | `test_audit_ledger_db.py::test_rolled_back_change_leaves_no_audit_entry` |
| SIG-01 | A signature records signer, timestamp and meaning | H | `test_signatures_db.py::test_signature_records_who_when_and_meaning` |
| SIG-02 | A signature is bound to one record version and does not transfer | H | `test_signatures_db.py`: `test_signature_verifies_for_the_signed_version`, `test_signature_does_not_transfer`, `test_altered_signature_record_is_detected` |
| SIG-03 | Signing needs a fresh re-authentication by the same user, used once | H | `test_signatures_db.py`: `test_signing_requires_fresh_reauthentication_by_the_signer`, `test_reauthentication_from_another_tenant_is_rejected`, `test_reauthentication_is_single_use`; `test_keycloak_realm_live.py::test_relogin_is_accepted_as_signing_reauthentication` |
| SIG-04 | Only authorised roles can apply each signature meaning | H | `test_signatures_db.py`: `test_role_must_match_meaning`, `test_medical_reviewer_can_medically_approve` |
| SIG-05 | Signatures are immutable and audited | H | `test_signatures_db.py`: `test_signatures_are_immutable`, `test_signing_writes_an_audit_entry` |
| OBS-01 | Logs and spans carry tenant_id; logs never include unlisted fields | L | `test_api_auth.py::test_request_spans_carry_tenant_id`; `test_logging.py` |
