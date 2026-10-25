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

## Phase 3: intake

| ID | Requirement | Risk | Evidence (tests) |
|---|---|---|---|
| INT-01 | The same email processed twice creates one case, including after re-delivery and a crash before it is marked seen | H | `apps/api/tests/test_email_intake.py`: `test_the_same_email_processed_twice_creates_one_case`, `test_redelivered_email_with_extra_headers_is_still_one_case`, `test_poller_reads_each_mailbox_message_into_one_case` (GreenMail) |
| INT-02 | Every intake endpoint needs an idempotency key; a repeat creates nothing; a reused key with other content is refused | H | `apps/api/tests/test_intake_api.py`: `test_report_with_the_same_key_creates_one_case`, `test_reusing_a_key_for_different_content_is_refused`, `test_intake_endpoints_require_an_idempotency_key` |
| INT-03 | Every source document is retrievable byte for byte, verified against its SHA-256 | H | `test_intake_api.py::test_uploaded_files_are_retrievable_byte_for_byte`; `test_email_intake.py::test_email_sources_are_kept_exactly`; `packages/platform/tests/test_blobstore.py::test_any_bytes_come_back_exactly` (Azurite, property-based) |
| INT-04 | Sources are immutable: never overwritten, tampering detected, records append-only | H | `test_blobstore.py`: `test_a_stored_blob_is_never_overwritten`, `test_reading_checks_the_recorded_hash`, `test_wrong_key_is_refused`; `test_intake_api.py::test_source_records_are_append_only` |
| INT-05 | Intake is audited with the hashes of all sources and announced through the outbox | H | `test_intake_api.py::test_intake_is_audited_with_source_hashes_and_announced` |
| INT-06 | Sources are isolated by tenant and data scope; downloads are never rendered inline | H | `test_intake_api.py`: `test_sources_are_isolated_by_tenant_and_scope`, `test_uploaded_files_are_retrievable_byte_for_byte` |
| INT-07 | Intake endpoints enforce roles | H | `test_intake_api.py::test_intake_needs_an_intake_role` |
| INT-08 | A document holding several reports becomes one case per report, each linked to its span | H | `test_intake_api.py::test_a_document_with_several_reports_becomes_several_cases`; `apps/api/tests/test_intake_text.py` (splitting, property-based) |
| INT-09 | Form fields become case values entered by the intake user; the system receipt date needs confirmation | H | `test_intake_api.py::test_form_fields_become_case_values_entered_by_the_intake_user` |
| INT-10 | Text extraction for plain text, HTML, PDF and images (OCR interface); other types are kept for manual entry | L | `test_intake_text.py` (extraction tests); real Tesseract: `test_tesseract_reads_the_text_of_an_image`, `test_tesseract_failure_marks_the_document_failed`, `test_tesseract_uses_only_installed_languages`; `test_intake_api.py::test_an_uploaded_image_is_read_by_tesseract_into_a_case` |
| INT-11 | Language detection for common report languages; undetermined rather than guessed | L | `test_intake_text.py`: `test_detects_common_report_languages`, `test_short_or_unclear_text_is_undetermined` |
| INT-12 | Email is stored as the raw message plus body and attachments | H | `test_intake_text.py::test_email_becomes_raw_message_body_and_attachments` |
| UI-01 | The web form sends one idempotency key per report and reuses it on retry | H | `apps/review_ui/src/IntakeForm.test.tsx` |

## Phase 4: workflow

| ID | Requirement | Risk | Evidence (tests) |
|---|---|---|---|
| WF-01 | The case workflow runs triage, data entry review, quality/medical review and approval, in order, for each version | H | `apps/workflow_worker/tests/test_case_workflow.py`: `test_serious_case_runs_every_review_step_with_four_eyes`, `test_invalid_case_is_held_until_it_becomes_valid` |
| WF-02 | A returned or edited case goes back to data entry, because an edit voids review signatures | H | `apps/workflow_worker/tests/test_case_workflow.py`::`test_returned_or_edited_case_goes_back_to_data_entry`; `apps/api/tests/test_review_api.py`::`test_an_edit_after_review_voids_the_review`, `test_reviewer_can_return_a_case_to_data_entry`; `packages/domain/tests/test_review.py`::`test_signatures_on_earlier_content_do_not_count` |
| WF-03 | Four eyes: the data entry reviewer cannot sign a later step of the same version | H | `packages/domain/tests/test_review.py`::`test_four_eyes_the_data_entry_reviewer_cannot_sign_later_steps`, `test_serious_case_approval_needs_quality_and_medical_review_by_others`; `apps/api/tests/test_review_api.py`::`test_four_eyes_the_data_entry_reviewer_cannot_approve`; `apps/workflow_worker/tests/test_case_workflow.py`::`test_serious_case_runs_every_review_step_with_four_eyes` |
| WF-04 | Review order and plan are enforced from signature records: serious cases need quality and medical review, non-serious cases are QC-sampled deterministically | H | `packages/domain/tests/test_review.py`: `test_later_steps_need_the_data_entry_review_first`, `test_plan_follows_seriousness_and_sampling`, `test_sampling_is_deterministic_and_respects_the_bounds`; `apps/api/tests/test_review_api.py`: `test_approval_needs_the_data_entry_review_first`, `test_serious_case_needs_quality_and_medical_review`, `test_sampled_non_serious_case_needs_a_quality_review` |
| WF-05 | Deadline escalations (warning, escalation, overdue) fire on time, once each, without moving the case (time-skipping test server) | H | `apps/workflow_worker/tests/test_case_workflow.py`::`test_deadline_escalations_fire_before_and_after_the_due_date` |
| WF-06 | Killing workers mid-case loses nothing and does not reset clocks | H | `apps/workflow_worker/tests/test_case_workflow.py`: `test_killing_the_worker_mid_case_loses_nothing_and_keeps_the_clocks`, `test_an_activity_that_crashes_after_committing_is_retried_without_duplicates` |
| WF-07 | Workflow versioning: recorded histories replay against the current code | H | `apps/workflow_worker/tests/test_case_workflow.py`::`test_recorded_histories_still_replay` (`tests/histories/`) |
| WF-08 | Case events reach the workflow as deduplicated signals-with-start | H | `apps/workflow_worker/tests/test_publisher.py`; `apps/api/tests/test_review_api.py`::`test_task_completion_is_announced_to_the_workflow` |
| TSK-01 | Work queues are filtered by role and data scope and sorted by deadline risk | H | `apps/api/tests/test_review_api.py`: `test_queue_is_ordered_by_deadline_risk_and_filtered_by_role`, `test_tasks_are_isolated_by_tenant_and_scope` |
| TSK-02 | Tasks can be claimed and assigned; only the assignee completes; completion is an e-signature | H | `apps/api/tests/test_review_api.py`::`test_claim_assign_and_only_the_assignee_completes` |
| TSK-03 | Opening a task is idempotent and replaces the previous open task; closed tasks cannot change | H | `apps/api/tests/test_review_api.py`: `test_opening_a_task_cancels_the_previous_one_and_is_idempotent`, `test_closed_tasks_cannot_change` |

## Phase 5: AI gateway and AI pipeline

| ID | Requirement | Risk | Evidence (tests) |
|---|---|---|---|
| AI-01 | The full pipeline runs on MockLLM: evidenced, unconfirmed proposals at L1, written as the AI pipeline actor, with the bundle version recorded | H | `apps/ai_worker/tests/test_pipeline.py::test_the_pipeline_prefills_evidenced_values_and_keeps_people_in_charge`; `apps/workflow_worker/tests/test_case_workflow.py::test_a_report_is_prefilled_by_the_ai_pipeline_before_data_entry` |
| AI-02 | A value without valid evidence is rejected: quote not in the source, quote without the value, invalid code, confidence out of range | H | `apps/ai_worker/tests/test_proposals.py::test_values_without_valid_evidence_or_codes_are_rejected`, `test_bad_dates_and_countries_are_rejected`, `test_invalid_output_is_rejected_whole`; `packages/domain/tests/test_ai_rules.py::test_evidence_must_be_a_verbatim_quote`, `test_free_text_values_must_be_in_their_quote` |
| AI-03 | Evidence points at exact offsets in the original document | H | `test_pipeline.py::test_the_pipeline_prefills_evidenced_values_and_keeps_people_in_charge`; `test_proposals.py::test_values_carry_evidence_in_original_document_offsets`; `packages/domain/tests/test_ai_redaction.py::test_restore_and_offsets_map_back_to_the_original` |
| AI-04 | AI never replaces a value a person entered | H | `test_pipeline.py::test_ai_values_never_replace_what_a_person_entered`; `test_proposals.py::test_existing_items_are_filled_not_duplicated_and_people_win` |
| AI-05 | Dual-signal seriousness: LLM OR rules; disagreement is flagged; a case either signal finds serious gets medical review, even at L0 | H | `test_ai_rules.py`: `test_rule_signal_finds_criteria`, `test_negated_mentions_do_not_count`, `test_signals_combine_with_or_and_flag_disagreement`, `test_review_plan_follows_the_ai_seriousness_flag`; `test_pipeline.py::test_when_both_providers_are_down_the_case_goes_on_in_l0_manual_mode`; `test_proposals.py::test_seriousness_is_proposed_only_when_met_and_never_as_not_met` |
| AI-06 | Coding is retrieval: the model can only pick a dictionary candidate | H | `test_proposals.py::test_coding_accepts_only_candidates`, `test_dictionary_search_is_robust_to_misspelling_and_synonyms`; `apps/ai_worker/tests/test_injection.py::test_a_fooled_model_cannot_change_the_outcome` |
| AI-07 | Duplicate detection: blocking keys plus pgvector similarity, top candidates with reasons, within the tenant | H | `test_pipeline.py::test_near_duplicates_are_reported_with_reasons`; `test_ai_rules.py`: `test_blocking_keys_are_normalised_and_skip_concomitant_drugs`, `test_vectors_are_deterministic_and_ignore_tokens`, `test_score_explains_itself` |
| AI-08 | Follow-up questions only for rule-found gaps, free of personal data | L | `test_proposals.py::test_follow_up_questions_must_match_a_gap_and_carry_no_personal_data`; `test_ai_rules.py::test_gaps_cover_validity_dates_doses_outcomes_and_seriousness` |
| AI-09 | Narrative draft from confirmed fields only, kept as a suggestion | H | `test_pipeline.py::test_the_narrative_draft_uses_confirmed_fields_only`; `test_case_workflow.py::test_a_report_is_prefilled_by_the_ai_pipeline_before_data_entry` |
| AI-10 | Automation levels L0/L1/L2 per tenant; serious cases capped at L1; L0 writes nothing to the case | H | `test_ai_rules.py`: `test_automation_level`, `test_serious_cases_never_go_beyond_l1`; `test_pipeline.py`: `test_l0_keeps_ai_output_as_suggestions_only`, `test_l2_is_for_non_serious_cases_only` |
| AI-11 | A provider outage falls back to the second provider, then to L0 manual mode; the case carries on | H | `test_pipeline.py`: `test_a_provider_outage_falls_back_to_the_second_provider`, `test_when_both_providers_are_down_the_case_goes_on_in_l0_manual_mode`; `apps/ai_gateway/tests/test_service.py`: `test_an_outage_falls_back_to_the_second_provider`, `test_when_every_provider_fails_the_caller_is_told` |
| AI-12 | Injection test suite: report text is delimited as data, no tools are offered, and a fooled model cannot change the outcome | H | `test_injection.py`: `test_a_fooled_model_cannot_change_the_outcome`, `test_report_text_is_delimited_and_no_tools_are_offered`; `test_service.py`: `test_report_text_is_delimited_as_data_with_a_random_id`, `test_text_cannot_close_the_data_block`; `test_providers.py::test_openai_request_has_strict_schema_pinned_model_and_no_tools` |
| AI-13 | Personal data is redacted before any model call, reversibly; the mapping is encrypted per tenant and record; no personal data in workflow history | H | `test_ai_redaction.py` (incl. property-based `test_redaction_is_always_reversible`); `packages/platform/tests/test_encryption.py`; `test_pipeline.py::test_the_pipeline_prefills_evidenced_values_and_keeps_people_in_charge` (decoded history, ciphertext) |
| AI-14 | The gateway refuses unredacted personal data, reporting kinds only | H | `test_service.py::test_unredacted_personal_data_is_refused_before_any_model_call`; `apps/ai_gateway/tests/test_api.py::test_gateway_errors_keep_their_code_over_http` |
| AI-15 | Gateway limits: retries with backoff, timeouts, max tokens and input size, schema-invalid output refused | H | `test_service.py`: `test_an_outage_falls_back_to_the_second_provider`, `test_output_that_is_not_valid_json_is_retried_then_refused`, `test_bad_requests`; `test_providers.py`: `test_http_errors_are_classified`, `test_timeouts_and_connection_failures_are_retryable`, `test_unusable_answers` |
| AI-16 | Per-tenant monthly budgets; every call recorded in an append-only ledger | H | `test_service.py`: `test_the_monthly_budget_is_enforced`, `test_calls_are_recorded_in_the_ledger_and_count_against_the_budget` |
| AI-17 | Pinned model versions: floating aliases refused in bundles, and a different served model is an error | H | `packages/platform/tests/test_aiconfig.py`: `test_floating_or_incomplete_model_targets_are_refused`, `test_production_bundle_pins_model_versions`; `test_providers.py::test_a_different_model_than_the_pinned_one_is_an_error` |
| AI-18 | The AI configuration is a signed bundle; tampered, unsigned or untrusted bundles are refused | H | `test_aiconfig.py`: `test_committed_bundles_are_signed_by_the_committed_key`, `test_tampering_after_signing_is_refused`, `test_unknown_signers_and_missing_signatures_are_refused`, `test_a_bundle_must_live_under_its_own_version` |
| AI-19 | Redacted call logging to Langfuse, one trace per case; failures never block a call | L | `test_api.py`: `test_langfuse_gets_one_trace_per_case_and_one_generation_per_call`, `test_langfuse_is_posted_in_the_background_and_failures_are_swallowed` |
| AI-20 | Pipeline steps are idempotent: a run is recorded once per case version | H | `test_pipeline.py::test_finalize_records_once`; `apps/api/tests/test_cases_api.py::test_ai_runs_are_readable_by_reviewers_and_isolated_by_tenant` |
| WF-09 | Workflow versioning across Phase 5: v1 and v2 histories replay (the AI pipeline and narrative are patched in) | H | `test_case_workflow.py::test_recorded_histories_still_replay` (`tests/histories/case_workflow_v1.json`, `case_workflow_v2.json`) |

## Phase 6: review UI

E2E = `apps/review_ui/e2e`, Playwright on the full stack with real Keycloak. UI = `apps/review_ui/src` unit tests (Vitest).

| ID | Requirement | Risk | Evidence (tests) |
|---|---|---|---|
| UI-02 | Intake to approval through the UI: intake, AI pre-fill, data entry review, approval by another person, each step e-signed | H | E2E `intake-to-approval.spec.ts` |
| UI-03 | Duplicate comparison and merge: picked values copied as unconfirmed imports, the duplicate closed, e-signed | H | E2E `duplicate-merge.spec.ts`; `apps/api/tests/test_review_ui_api.py`: `test_merge_copies_picked_values_signs_and_closes_the_duplicate`, `test_merging_into_an_approved_case_starts_a_follow_up`, `test_a_merge_is_final_and_checked`, `test_an_approved_case_cannot_be_merged_away`; `apps/workflow_worker/tests/test_case_workflow.py::test_a_merged_duplicate_stops_its_workflow` |
| UI-04 | Follow-up versioning: a new version from an approved one, which stays read-only and unchanged | H | E2E `follow-up.spec.ts` |
| UI-05 | E-signature dialog: states subject and meaning, re-authenticates first, signs nothing if re-authentication fails | H | UI `components.test.tsx`: `signing re-authenticates first and sends the fresh token with the reason`, `a cancelled re-authentication signs nothing and says why`; E2E (every signature) |
| UI-06 | Evidence highlighting in the source document, with the field's own span marked | H | UI `components.test.tsx`: `evidence segments split text at highlight boundaries`, `the source panel marks evidence and the active field`; E2E `intake-to-approval.spec.ts`; `test_review_ui_api.py::test_the_source_text_of_a_case_from_intake` |
| UI-07 | Field provenance and confidence badges (low confidence flagged); confirm and edit record a person's action | H | UI `components.test.tsx`: `a field shows provenance, low confidence, and confirms on request`, `an edited value is saved as a person's value` |
| UI-08 | Queues sorted by deadline risk, with AI flags, within the user's roles and scope | H | UI `App.test.tsx::lists open tasks, most urgent first, with deadline risk and AI flags`, `components.test.tsx::deadline risk reads days left to the due date`; `test_review_ui_api.py::test_the_queue_shows_stage_and_ai_flags`; `test_review_api.py::test_queue_is_ordered_by_deadline_risk_and_filtered_by_role` |
| UI-09 | Seriousness panel shows the LLM and rule signals; every criterion assessed by a person before approval | H | E2E `intake-to-approval.spec.ts` (approval blocked until assessed); `test_ai_rules.py::test_signals_combine_with_or_and_flag_disagreement` |
| UI-10 | Coding picker offers dictionary candidates only | H | `test_review_ui_api.py::test_coding_search_returns_dictionary_candidates`; `test_proposals.py::test_coding_accepts_only_candidates` |
| UI-11 | Case audit trail and version history readable by the case's reviewers, isolated by tenant | H | `test_review_ui_api.py::test_a_cases_audit_trail_is_readable_by_its_reviewers`; E2E `intake-to-approval.spec.ts`, `follow-up.spec.ts` |
| UI-12 | Tenant admin settings: validated, audited per field, own tenant only, tenant admin role only | H | `test_review_ui_api.py`: `test_a_tenant_admin_changes_their_settings_with_an_audit_trail`, `test_invalid_settings_are_refused`, `test_only_tenant_admins_and_only_their_own_row`; E2E (QC setting in `beforeAll`) |
| UI-13 | WCAG 2.1 AA: no automated violations on intake, queue, workspace, history and admin | H | E2E `accessibility.spec.ts` (axe-core, tags wcag2a/aa, wcag21a/aa) |
| UI-14 | Sign-in exchanges the authorization code once (React StrictMode safe) | L | E2E sign-in in every spec |
