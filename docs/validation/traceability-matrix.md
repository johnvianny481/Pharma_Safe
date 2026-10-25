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

## Phase 2: case domain

| ID | Requirement | Risk | Evidence (tests) |
|---|---|---|---|
| CAS-01 | Case model mirrors the main E2B(R3) sections and rejects structurally impossible values | H | `packages/domain/tests/test_case_model.py` |
| CAS-02 | Every field stores value, source, evidence span, confidence, confirmed_by, confirmed_at; AI values need evidence and confidence | H | `packages/domain/tests/test_provenance.py` |
| CAS-03 | Clients cannot set provenance; API values are human and confirmed by the caller; confirm keeps the value and evidence | H | `apps/api/tests/test_cases_api.py`: `test_values_entered_through_the_api_are_human_and_confirmed`, `test_clients_cannot_forge_provenance`, `test_confirm_keeps_the_value_and_records_the_confirmer` |
| CAS-04 | Approved versions are immutable for every role; versions are never deleted | H | `test_cases_api.py::test_approved_version_is_immutable` |
| CAS-05 | Follow-up information creates a new version and leaves the approved one unchanged | H | `test_cases_api.py::test_follow_up_creates_a_new_version_and_leaves_the_approved_one_intact` |
| CAS-06 | A past case version can be rebuilt exactly, as it was at any moment, from the audit trail | H | `apps/api/tests/test_case_rebuild_db.py::test_every_past_state_rebuilds_exactly` (property-based); `test_cases_api.py::test_past_version_is_rebuilt_exactly_from_the_audit_trail`; `packages/domain/tests/test_changes.py` |
| CAS-07 | Concurrent edits cannot overwrite each other; invalid changes apply nothing | H | `test_cases_api.py`: `test_stale_revision_is_rejected`, `test_invalid_changes_are_rejected_atomically` |
| CAS-08 | Every case operation is audited in the hash chain, with field, old and new value, reason and case version | H | `test_cases_api.py::test_every_case_operation_is_audited_in_a_valid_chain` |
| CAS-09 | Approval requires an e-signature with fresh re-authentication, an approver role, and no blockers | H | `test_cases_api.py`: `test_approval_needs_the_right_role_and_a_fresh_reauthentication`, `test_incomplete_case_is_invalid_and_cannot_be_approved`; `packages/domain/tests/test_checks.py` |
| VAL-01 | Validity requires the four minimum criteria | H | `packages/domain/tests/test_validity.py` (property-based, against an independent oracle) |
| CHK-01 | Date checks flag only definite contradictions (future, before 1900, end before start, before birth, receipt order) | H | `packages/domain/tests/test_checks.py`, `test_dates.py` |
| CHK-02 | Unit and range checks (age, weight, height, dose units, test ranges) | L | `test_checks.py` |
| CLK-01 | Day 0 is the receipt date of the first valid state and is never changed | H | `packages/domain/tests/test_clocks.py::test_day_zero_is_first_valid_receipt`, `test_no_clock_starts_before_day_zero`; `test_cases_api.py::test_day_zero_and_clocks_cannot_be_reset` |
| CLK-02 | Due dates come from versioned rule sets per regulator; the set in force at clock start applies | H | `test_clocks.py`: `test_due_date_is_clock_start_plus_the_rule_period`, `test_rule_set_in_force_on_the_clock_start_applies` |
| CLK-03 | Seeded rules are clearly marked as configurable examples | H | `test_clocks.py::test_seeded_rule_sets_are_marked_as_examples` |
| CLK-04 | Clocks never reset; later information can only start new clocks | H | `test_clocks.py`: `test_clocks_never_reset_as_information_arrives`, `test_resuming_from_stored_clocks_matches_a_full_evaluation`, `test_follow_up_that_makes_a_case_serious_starts_a_new_clock` |
| CLK-05 | Unknown report type, country or expectedness is treated as reportable | H | `test_clocks.py`: `test_unknown_country_is_treated_as_domestic_and_foreign`, `test_unknown_report_type_matches_both_settings`, `test_unassessed_expectedness_counts_as_unexpected` |
| TEN-06 | Cases are isolated between tenants through the API | H | `test_cases_api.py::test_cases_are_isolated_between_tenants`; `test_tenant_isolation_db.py::test_every_tenant_table_has_forced_rls` |
| IAM-06 | Data scopes by product and region filter cases and fail closed | H | `test_cases_api.py::test_data_scopes_limit_which_cases_a_user_sees` |
| IAM-07 | Case endpoints enforce roles | H | `test_cases_api.py::test_roles_are_enforced` |
| OUT-01 | An event commits or rolls back with its change | H | `packages/platform/tests/test_outbox_db.py::test_event_commits_or_rolls_back_with_the_change` |
| OUT-02 | Events are relayed in order per tenant, once on success, and retried after failure without overtaking | H | `test_outbox_db.py`: `test_relay_publishes_each_tenants_events_in_order_once`, `test_failed_publish_is_recorded_and_later_events_wait`, `test_app_role_can_only_mark_events_published` |
| OBS-02 | Case request spans carry case_id and tenant_id | L | `test_cases_api.py::test_case_spans_carry_case_id` |
