"""The full AI pipeline on MockLLM: intake text in, evidenced proposals out.

Runs on Temporal's test server against the real database and the gateway service in
process, with every control in place: redaction, evidence, both seriousness signals,
retrieval-only coding, duplicates, automation levels and the L0 fallback.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from temporalio.exceptions import ApplicationError

from pharmasafe_ai_worker.activities import Deps, PipelineActivities
from pharmasafe_ai_worker.contracts import FinalizeInput, NarrativeInput, Outputs, PipelineInput
from pharmasafe_api.cases import ai as case_ai
from pharmasafe_api.cases import service as cases
from pharmasafe_api.cases.models import CaseRow
from pharmasafe_api.intake import service as intake
from pharmasafe_domain.case import SafetyCase
from pharmasafe_domain.changes import SetField
from pharmasafe_domain.provenance import Tracked
from pharmasafe_domain.rules.clocks import seed_registry
from pharmasafe_platform.audit import Actor
from pharmasafe_platform.db import Database

REPORT = (
    "Reporter: Dr Maria Silva (maria.silva@clinic.example, +44 20 7946 0958).\n"
    "Patient JD, a 64-year-old man, developed a severe rash on 2026-10-01 after starting"
    " Acmezol 20 mg. He was hospitalised for two days and has recovered."
)
NON_SERIOUS = (
    "Reporter: Dr Maria Silva. Patient JD, a 64-year-old man, developed a mild rash on"
    " 2026-10-01 after starting Acmezol 20 mg."
)
PERSONAL = ("Maria Silva", "maria.silva@clinic.example", "+44 20 7946 0958")


def _source(db: Database, tenant: UUID, case_id: UUID) -> intake.CaseSource:
    with db.tenant_session(tenant) as session:
        source = intake.source_of_case(session, case_id)
    assert source is not None
    return source


def _run(db: Database, tenant: UUID, case_id: UUID) -> Any:
    with db.tenant_session(tenant) as session:
        return case_ai.run_of(session, case_id, 1, case_ai.PIPELINE)


def _ai_values(node: Any, path: str = "") -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    if isinstance(node, dict):
        if node.get("source") == "ai":
            return [(path, node)]
        for key, child in node.items():
            found += _ai_values(child, f"{path}/{key}")
    elif isinstance(node, list):
        for item in node:
            found += _ai_values(item, f"{path}/{item.get('id') if isinstance(item, dict) else ''}")
    return found


def test_the_pipeline_prefills_evidenced_values_and_keeps_people_in_charge(
    db: Database,
    tenant_a: UUID,
    pipeline_deps: Deps,
    ingest_report: Callable[[UUID, str], UUID],
    run_pipelines: Callable[..., list[Any]],
    case_content: Callable[[UUID, UUID], Any],
) -> None:
    case_id = ingest_report(tenant_a, REPORT)
    (run,) = run_pipelines(pipeline_deps, [(tenant_a, case_id)])
    assert run.summary.status == "completed"
    assert run.summary.automation_level == "L1"
    assert run.summary.bundle_version == "mock-2026.10.1"
    assert run.summary.applied_changes > 0

    content = case_content(tenant_a, case_id)
    assert content.patient.initials is not None
    assert content.patient.initials.value == "JD"
    assert content.patient.sex is not None
    assert content.patient.sex.value.value == "male"
    (reporter,) = content.reporters
    assert reporter.family_name is not None
    assert reporter.family_name.value == "Maria Silva"  # restored from its token
    assert reporter.qualification is not None
    assert reporter.qualification.value.value == "physician"
    (drug,) = content.drugs
    assert drug.product_name is not None
    assert drug.product_name.value == "Acmezol"
    assert drug.dose is not None
    assert str(drug.dose.value.value) == "20"
    (reaction,) = content.reactions
    assert reaction.reported_term is not None
    assert reaction.reported_term.value == "rash"
    assert reaction.onset_date is not None
    assert reaction.onset_date.value == "2026-10-01"
    assert reaction.meddra_code is not None
    assert reaction.meddra_code.value == "90000001"  # from the dictionary candidates
    assert reaction.seriousness.hospitalisation is not None
    assert reaction.seriousness.hospitalisation.value is True

    # Every AI value cites the original document, exactly, and is not yet confirmed.
    source = _source(db, tenant_a, case_id)
    with db.tenant_session(tenant_a) as session:
        document = intake.texts_of(session, [source.document_id])[source.document_id].text
    values = _ai_values(content.to_json())
    assert len(values) >= 12
    for path, value in values:
        evidence = value["evidence"]
        assert document[evidence["start"] : evidence["end"]] == evidence["text"], path
        assert value["confirmed_by"] is None, path
        assert 0 <= value["confidence"] <= 1, path
    assert reporter.family_name.evidence is not None
    assert reporter.family_name.evidence.text == "Maria Silva"

    record = _run(db, tenant_a, case_id)
    assert record.bundle_version == "mock-2026.10.1"
    assert cases.SERIOUS_BY_SIGNALS in record.flags
    assert record.result["seriousness"]["met"]["hospitalisation"] is True
    assert record.result["seriousness"]["disagreements"] == []
    assert {c["code"] for c in record.result["coding"]} >= {"90000001", "SYN-WD-0001"}
    questions = {q["path"] for q in record.result["follow_up_questions"]}
    assert "/drugs/ai-d1/start_date" in questions
    assert record.result["dictionary_releases"]["meddra_llt"] == "SYNTH-MEDDRA-1.0"

    # No personal data in Temporal's workflow history: only tokens travel through it.
    for value in PERSONAL:
        assert value not in run.history
    assert "[NAME_1]" in run.history

    # The mapping is stored encrypted, and AI changes are audited as the AI pipeline.
    with db.tenant_session(tenant_a) as session:
        (cipher_row,) = session.execute(
            text("SELECT ciphertext FROM case_redactions WHERE case_id = :c"), {"c": case_id}
        ).one()
        actors = session.scalars(
            text("SELECT DISTINCT actor_id FROM audit_ledger WHERE entity_id = :c"
                 " AND action = 'case.field_set'"), {"c": str(case_id)},
        ).all()  # fmt: skip
        steps = session.scalars(
            text("SELECT step FROM ai_calls WHERE case_id = :c ORDER BY created_at"),
            {"c": str(case_id)},
        ).all()
    assert b"Maria" not in bytes(cipher_row)
    assert actors == ["system:ai-pipeline"]
    assert steps == ["extraction", "seriousness", "coding", "follow_up"]


def test_ai_values_never_replace_what_a_person_entered(
    db: Database,
    tenant_a: UUID,
    pipeline_deps: Deps,
    ingest_report: Callable[[UUID, str], UUID],
    run_pipelines: Callable[..., list[Any]],
    case_content: Callable[[UUID, UUID], Any],
) -> None:
    case_id = ingest_report(tenant_a, REPORT)
    with db.tenant_session(tenant_a) as session:
        case = session.get_one(CaseRow, case_id)
        version = cases.get_version(session, case_id, 1)
        initials = Tracked[str].human("AB", by="proc-1", at=datetime.now(UTC))
        cases.apply_system_changes(
            session, case, 1, expected_revision=version.revision,
            changes=[SetField(path="/patient/initials", value=initials.model_dump(mode="json"))],
            actor=Actor("proc-1", "Processor"),
            ctx=cases.CaseContext(seed_registry(), datetime.now(UTC)), reason="entered",
        )  # fmt: skip
    run_pipelines(pipeline_deps, [(tenant_a, case_id)])
    initials_now = case_content(tenant_a, case_id).patient.initials
    assert initials_now is not None
    assert (initials_now.value, initials_now.source.value) == ("AB", "human")


def test_l0_keeps_ai_output_as_suggestions_only(
    db: Database,
    tenant_a: UUID,
    pipeline_deps: Deps,
    ingest_report: Callable[[UUID, str], UUID],
    run_pipelines: Callable[..., list[Any]],
    case_content: Callable[[UUID, UUID], Any],
    set_automation: Callable[[UUID, str], None],
) -> None:
    set_automation(tenant_a, "L0")
    case_id = ingest_report(tenant_a, REPORT)
    (run,) = run_pipelines(pipeline_deps, [(tenant_a, case_id)])
    assert (run.summary.automation_level, run.summary.applied_changes) == ("L0", 0)
    assert case_content(tenant_a, case_id).drugs == ()
    assert _run(db, tenant_a, case_id).result["proposals"]  # for the side panel


def test_l2_is_for_non_serious_cases_only(
    db: Database,
    tenant_a: UUID,
    make_bundles: Callable[..., Any],
    make_deps: Callable[..., Deps],
    ingest_report: Callable[[UUID, str], UUID],
    run_pipelines: Callable[..., list[Any]],
    set_automation: Callable[[UUID, str], None],
) -> None:
    set_automation(tenant_a, "L2")
    bundles, version = make_bundles("l2-1", thresholds={"straight_through_confidence": 0.5})
    serious = ingest_report(tenant_a, REPORT)
    simple = ingest_report(tenant_a, NON_SERIOUS)
    runs = run_pipelines(make_deps(bundles, version), [(tenant_a, serious), (tenant_a, simple)])
    assert [r.summary.automation_level for r in runs] == ["L1", "L2"]
    assert (
        "serious cases are capped at L1"
        in _run(db, tenant_a, serious).result["automation"]["reasons"]
    )
    assert "straight_through" in runs[1].summary.flags


def test_a_provider_outage_falls_back_to_the_second_provider(
    db: Database,
    tenant_a: UUID,
    make_bundles: Callable[..., Any],
    make_deps: Callable[..., Deps],
    ingest_report: Callable[[UUID, str], UUID],
    run_pipelines: Callable[..., list[Any]],
) -> None:
    bundles, version = make_bundles("outage-1", primary="mock-outage-v1", max_attempts=2)
    case_id = ingest_report(tenant_a, REPORT)
    (run,) = run_pipelines(make_deps(bundles, version), [(tenant_a, case_id)])
    assert (run.summary.status, run.summary.automation_level) == ("completed", "L1")
    with db.tenant_session(tenant_a) as session:
        outcomes = set(
            session.scalars(
                text("SELECT outcome FROM ai_calls WHERE case_id = :c"), {"c": str(case_id)}
            )
        )
    assert outcomes == {"fallback_ok"}


def test_when_both_providers_are_down_the_case_goes_on_in_l0_manual_mode(
    db: Database,
    tenant_a: UUID,
    make_bundles: Callable[..., Any],
    make_deps: Callable[..., Deps],
    ingest_report: Callable[[UUID, str], UUID],
    run_pipelines: Callable[..., list[Any]],
    case_content: Callable[[UUID, UUID], Any],
) -> None:
    bundles, version = make_bundles(
        "down-1", primary="mock-outage-v1", fallback="mock-outage-v1", max_attempts=1
    )
    case_id = ingest_report(tenant_a, REPORT)
    (run,) = run_pipelines(make_deps(bundles, version), [(tenant_a, case_id)])
    assert (run.summary.status, run.summary.automation_level) == ("manual_fallback", "L0")
    assert "ai_unavailable" in run.summary.flags
    assert case_content(tenant_a, case_id).reactions == ()  # people enter the data
    record = _run(db, tenant_a, case_id)
    assert "providers_unavailable" in record.result["unavailable_reason"]
    # The rule signal still sees "hospitalised": the case is reviewed as serious.
    assert cases.SERIOUS_BY_SIGNALS in run.summary.flags
    with db.tenant_session(tenant_a) as session:
        case = session.get_one(CaseRow, case_id)
        plan = cases.review_plan_of(session, case, cases.get_version(session, case_id, 1))
    assert plan.medical


def test_near_duplicates_are_reported_with_reasons(
    db: Database,
    tenant_a: UUID,
    tenant_b: UUID,
    pipeline_deps: Deps,
    ingest_report: Callable[[UUID, str], UUID],
    run_pipelines: Callable[..., list[Any]],
) -> None:
    first = ingest_report(tenant_a, REPORT)
    resent = ingest_report(tenant_a, REPORT.replace("two days", "2 days"))
    elsewhere = ingest_report(tenant_b, REPORT)
    runs = run_pipelines(
        pipeline_deps, [(tenant_a, first), (tenant_a, resent), (tenant_b, elsewhere)]
    )
    assert "duplicate_candidates" not in runs[0].summary.flags
    assert "duplicate_candidates" in runs[1].summary.flags
    (candidate,) = _run(db, tenant_a, resent).result["duplicates"]
    assert candidate["case_id"] == str(first)
    assert "same suspect drug: acmezol" in candidate["reasons"]
    assert "same reaction: rash" in candidate["reasons"]
    assert candidate["score"] > 0.9
    # Another tenant's identical report is never a candidate (row-level security).
    assert "duplicate_candidates" not in runs[2].summary.flags


def test_finalize_records_once(
    db: Database,
    tenant_a: UUID,
    pipeline_deps: Deps,
    ingest_report: Callable[[UUID, str], UUID],
    run_pipelines: Callable[..., list[Any]],
) -> None:
    case_id = ingest_report(tenant_a, REPORT)
    (run,) = run_pipelines(pipeline_deps, [(tenant_a, case_id)])
    with db.tenant_session(tenant_a) as session:
        revision = cases.get_version(session, case_id, 1).revision
    again = PipelineActivities(pipeline_deps).finalize(
        FinalizeInput(str(tenant_a), str(case_id), 1, "mock-2026.10.1", Outputs(), True)
    )
    assert again.automation_level == run.summary.automation_level
    with db.tenant_session(tenant_a) as session:
        assert cases.get_version(session, case_id, 1).revision == revision


def test_the_narrative_draft_uses_confirmed_fields_only(
    db: Database,
    tenant_a: UUID,
    pipeline_deps: Deps,
    ingest_report: Callable[[UUID, str], UUID],
    run_pipelines: Callable[..., list[Any]],
) -> None:
    case_id = ingest_report(tenant_a, REPORT)
    run_pipelines(pipeline_deps, [(tenant_a, case_id)])
    with db.tenant_session(tenant_a) as session:
        case = session.get_one(CaseRow, case_id)
        version = cases.get_version(session, case_id, 1)
        weight = Tracked[str].human("81", by="proc-1", at=datetime.now(UTC))
        cases.apply_system_changes(
            session, case, 1, expected_revision=version.revision,
            changes=[SetField(path="/patient/weight_kg", value=weight.model_dump(mode="json"))],
            actor=Actor("proc-1", "Processor"),
            ctx=cases.CaseContext(seed_registry(), datetime.now(UTC)), reason="entered",
        )  # fmt: skip
    activities = PipelineActivities(pipeline_deps)
    assert activities.draft_narrative(NarrativeInput(str(tenant_a), str(case_id), 1)) == "drafted"
    assert activities.draft_narrative(NarrativeInput(str(tenant_a), str(case_id), 1)) == "exists"
    with db.tenant_session(tenant_a) as session:
        draft = case_ai.run_of(session, case_id, 1, case_ai.NARRATIVE)
    assert draft is not None
    assert "/patient/weight_kg: 81" in draft.result["draft"]
    assert "Acmezol" not in draft.result["draft"]  # an unconfirmed AI value
    assert draft.result["facts_used"] == 1


def test_a_case_without_readable_text_is_recorded_without_model_calls(
    db: Database,
    tenant_a: UUID,
    pipeline_deps: Deps,
    run_pipelines: Callable[..., list[Any]],
) -> None:
    with db.tenant_session(tenant_a) as session:
        case, _ = cases.create_case(
            session, tenant_a, Actor("proc-0", "Intake"), SafetyCase(),
            cases.CaseContext(seed_registry(), datetime.now(UTC)),
        )  # fmt: skip
        case_id = case.id
    (run,) = run_pipelines(pipeline_deps, [(tenant_a, case_id)])
    assert "no_source_text" in run.summary.flags
    with db.tenant_session(tenant_a) as session:
        calls = session.scalar(
            text("SELECT count(*) FROM ai_calls WHERE case_id = :c"), {"c": str(case_id)}
        )
    assert calls == 0


def test_an_untrusted_bundle_stops_the_pipeline_at_once(
    tenant_a: UUID,
    committed_bundles: Any,
    make_deps: Callable[..., Deps],
    ingest_report: Callable[[UUID, str], UUID],
) -> None:
    case_id = ingest_report(tenant_a, REPORT)
    activities = PipelineActivities(make_deps(committed_bundles, "no-such-bundle"))
    with pytest.raises(ApplicationError) as error:
        activities.prepare(PipelineInput(str(tenant_a), str(case_id), 1))
    assert (error.value.type, error.value.non_retryable) == ("BundleRefused", True)
