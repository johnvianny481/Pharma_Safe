"""Activities of the AI pipeline.

| Activity | Does | Model call |
|---|---|---|
| ``prepare`` | redacts the report text, stores the encrypted mapping | no |
| ``extract`` | structured extraction with evidence | yes |
| ``plan`` | checks the extraction, lists reactions, coding items and gaps | no |
| ``assess_seriousness`` | the LLM seriousness signal | yes |
| ``code_terms`` | picks codes from dictionary candidates | yes |
| ``ask_follow_up`` | words follow-up questions for the gaps | yes |
| ``finalize`` | evidence, both signals, coding, duplicates, level; writes case and run | no |
| ``draft_narrative`` | a narrative draft from confirmed fields only | yes |

Every activity is safe to retry. ``prepare`` reuses a stored redaction, ``finalize`` and
``draft_narrative`` record once per case version, and model calls are idempotent reads.
When the gateway says no provider is available (or the budget is spent), a model-call
activity fails with the non-retryable ``AiUnavailable`` error and the pipeline workflow
finishes the case in L0 manual mode.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from pharmasafe_ai_gateway.client import UNREACHABLE, Gateway
from pharmasafe_ai_gateway.schemas import NarrativeOut
from pharmasafe_ai_gateway.service import GatewayError, StepCall
from pharmasafe_ai_worker.contracts import (
    FinalizeInput,
    NarrativeInput,
    PipelineInput,
    PipelineSummary,
    Plan,
    PlanInput,
    Prepared,
    StepInput,
)
from pharmasafe_ai_worker.dictionary import Dictionary, Kind
from pharmasafe_ai_worker.proposals import (
    EvidenceMapper,
    Proposals,
    accept_questions,
    apply_coding,
    apply_seriousness,
    build_extraction,
    coding_items,
)
from pharmasafe_api.cases import ai as case_ai
from pharmasafe_api.cases import service as cases
from pharmasafe_api.cases.models import CaseRow
from pharmasafe_api.intake import service as intake
from pharmasafe_domain.ai.automation import Level, effective_level
from pharmasafe_domain.ai.duplicates import (
    Candidate,
    blocking_keys,
    score,
    top_candidates,
    vectorize,
)
from pharmasafe_domain.ai.followup import missing_information
from pharmasafe_domain.ai.redaction import TOKEN, Redaction, redact
from pharmasafe_domain.ai.seriousness import rule_signal
from pharmasafe_domain.case import SafetyCase
from pharmasafe_domain.rules.clocks import RuleSetRegistry
from pharmasafe_domain.rules.seriousness import case_is_serious
from pharmasafe_domain.rules.validity import assess_validity
from pharmasafe_platform.aiconfig import Bundle, BundleError, BundleStore, Step
from pharmasafe_platform.db import Database
from pharmasafe_platform.encryption import TenantCipher

AI_UNAVAILABLE = "AiUnavailable"


@dataclass(frozen=True)
class Deps:
    database: Database
    gateway: Gateway
    bundles: BundleStore
    bundle_version: str
    cipher: TenantCipher
    dictionary: Dictionary
    registry: RuleSetRegistry
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


class PipelineActivities:
    def __init__(self, deps: Deps) -> None:
        self._d = deps

    # ------------------------------------------------------------ preparation

    @activity.defn(name="prepare")
    def prepare(self, inp: PipelineInput) -> Prepared:
        bundle_version = self._d.bundle_version
        try:  # refuse to start on an untrusted bundle; retrying cannot fix it
            self._d.bundles.get(bundle_version)
        except BundleError as exc:
            raise ApplicationError(str(exc), type="BundleRefused", non_retryable=True) from exc
        with self._d.database.tenant_session(UUID(inp.tenant_id)) as session:
            case = session.get_one(CaseRow, UUID(inp.case_id))
            redaction, source = self._redaction(session, case, inp.version, store=True)
        if redaction is None or source is None:
            return Prepared(bundle_version, None, None)
        return Prepared(bundle_version, str(source.document_id), redaction.text)

    def _redaction(
        self, session: Any, case: CaseRow, version: int, *, store: bool
    ) -> tuple[Redaction | None, intake.CaseSource | None]:
        source = intake.source_of_case(session, case.id)
        if source is None:
            return None, None
        document_id = str(source.document_id)
        stored = case_ai.reveal_redaction(session, case, version, document_id, self._d.cipher)
        if stored is not None:
            return Redaction.from_json(stored), source
        content = cases.case_of(cases.get_version(session, case.id, version))
        redaction = redact(source.text, case_ai.personal_values(content))
        if store:
            case_ai.store_redaction(
                session, case, version, document_id, redaction.to_json(), self._d.cipher,
                self._d.clock(),
            )  # fmt: skip
        return redaction, source

    # ------------------------------------------------------------ model calls

    def _call(self, step: Step, inp: StepInput) -> dict[str, Any]:
        call = StepCall(UUID(inp.tenant_id), inp.case_id, step, inp.bundle_version, inp.variables)
        try:
            return self._d.gateway.run_step(call).output
        except GatewayError as exc:
            if exc.code == UNREACHABLE:
                raise ApplicationError(str(exc), type="GatewayUnreachable") from exc
            # Outage, budget, or a refusal (which is a bug upstream, e.g. unredacted
            # text): no retry helps. The case continues in L0 manual mode.
            raise ApplicationError(
                f"{exc.code}: {exc}", type=AI_UNAVAILABLE, non_retryable=True
            ) from exc

    @activity.defn(name="extract")
    def extract(self, inp: StepInput) -> dict[str, Any]:
        return self._call(Step.EXTRACTION, inp)

    @activity.defn(name="assess_seriousness")
    def assess_seriousness(self, inp: StepInput) -> dict[str, Any]:
        return self._call(Step.SERIOUSNESS, inp)

    @activity.defn(name="code_terms")
    def code_terms(self, inp: StepInput) -> dict[str, Any]:
        return self._call(Step.CODING, inp)

    @activity.defn(name="ask_follow_up")
    def ask_follow_up(self, inp: StepInput) -> dict[str, Any]:
        return self._call(Step.FOLLOW_UP, inp)

    # ------------------------------------------------------------ planning

    @activity.defn(name="plan")
    def plan(self, inp: PlanInput) -> Plan:
        """What the later model calls need, from redacted values only (no personal data)."""
        bundle = self._d.bundles.get(inp.prepared.bundle_version)
        with self._d.database.tenant_session(UUID(inp.tenant_id)) as session:
            content = cases.case_of(cases.get_version(session, UUID(inp.case_id), inp.version))
        mapper = EvidenceMapper.redacted_only(
            inp.prepared.redacted_text or "", inp.prepared.document_id or "none"
        )
        proposals = build_extraction(content, inp.extraction, mapper)
        return Plan(
            reactions=[
                {"id": r.id, "term": r.reported_term.value}
                for r in proposals.case.reactions
                if r.reported_term is not None
            ],
            coding_items=coding_items(
                proposals.case, self._d.dictionary, bundle.retrieval.coding_candidates
            ),
            gaps=[g.__dict__ for g in missing_information(proposals.case)],
        )

    # ------------------------------------------------------------ the result

    @activity.defn(name="finalize")
    def finalize(self, inp: FinalizeInput) -> PipelineSummary:
        bundle = self._d.bundles.get(inp.bundle_version)
        now = self._d.clock()
        with self._d.database.tenant_session(UUID(inp.tenant_id)) as session:
            case = session.get_one(CaseRow, UUID(inp.case_id))
            existing = case_ai.run_of(session, case.id, inp.version, case_ai.PIPELINE)
            if existing is not None:
                return _summary(existing, applied=existing.result.get("applied_changes", 0))
            version = cases.get_version(session, case.id, inp.version)
            content = cases.case_of(version)
            redaction, source = self._redaction(session, case, inp.version, store=False)
            outcome = self._evaluate(session, case, content, redaction, source, bundle, inp)
            level = outcome["decision"].level
            applied = 0
            if (
                level is not Level.L0
                and outcome["proposals"].changes
                and version.status == cases.DRAFT
            ):
                cases.apply_system_changes(
                    session,
                    case,
                    inp.version,
                    expected_revision=version.revision,
                    changes=outcome["proposals"].changes,
                    actor=case_ai.AI_ACTOR,
                    ctx=cases.CaseContext(registry=self._d.registry, now=now),
                    reason=f"AI pipeline, bundle {inp.bundle_version}, level {level.value}",
                )
                applied = len(outcome["proposals"].changes)
            result = _result(outcome, applied, inp)
            status = "completed" if inp.ai_available else "manual_fallback"
            case_ai.record_run(
                session, case, inp.version, kind=case_ai.PIPELINE,
                bundle_version=inp.bundle_version, status=status, level=level,
                flags=outcome["flags"], result=result, now=now,
            )  # fmt: skip
        return PipelineSummary(
            inp.case_id, inp.version, inp.bundle_version, status, level.value,
            outcome["flags"], applied,
        )  # fmt: skip

    def _evaluate(
        self,
        session: Any,
        case: CaseRow,
        content: SafetyCase,
        redaction: Redaction | None,
        source: intake.CaseSource | None,
        bundle: Bundle,
        inp: FinalizeInput,
    ) -> dict[str, Any]:
        outputs = inp.outputs
        if redaction is not None and source is not None:
            mapper = EvidenceMapper(
                redaction, document_id=str(source.document_id), original=source.text,
                offset=source.offset, restore=True,
            )  # fmt: skip
        else:
            mapper = EvidenceMapper.redacted_only("", "none")
        if outputs.extraction is not None:
            proposals = build_extraction(content, outputs.extraction, mapper)
        else:
            proposals = Proposals(case=content)
        gaps = [g.__dict__ for g in missing_information(proposals.case)]
        rules = rule_signal(redaction.text) if redaction is not None else {}
        seriousness = apply_seriousness(proposals, outputs.seriousness, rules, mapper)
        items = coding_items(proposals.case, self._d.dictionary, bundle.retrieval.coding_candidates)
        coding = apply_coding(proposals, outputs.coding, items)
        questions = accept_questions(proposals, outputs.follow_up, gaps)

        keys = blocking_keys(proposals.case)
        vector = vectorize(
            redaction.text if redaction is not None else "",
            n=bundle.retrieval.ngram,
            dimensions=bundle.retrieval.dimensions,
        )
        case_ai.upsert_vector(session, case, inp.version, keys, vector, self._d.clock())
        similar = case_ai.similar_cases(
            session, case, keys, vector, limit=bundle.retrieval.duplicate_block_limit
        )
        candidates = []
        for other_id, number, other_keys, similarity in similar:
            points, reasons = score(keys, other_keys, similarity)
            candidates.append(Candidate(other_id, number, points, reasons))
        duplicates = top_candidates(
            candidates,
            top=bundle.retrieval.duplicate_top,
            min_score=bundle.thresholds.duplicate_min_score,
        )

        serious = seriousness.serious or case_is_serious(proposals.case)
        confidences = list(proposals.confidences.values())
        decision = effective_level(
            case_ai.automation_level_of(session, case.tenant_id),
            ai_available=inp.ai_available,
            serious=serious,
            valid=assess_validity(proposals.case).valid,
            min_confidence=min(confidences) if confidences else None,
            threshold=bundle.thresholds.straight_through_confidence,
        )
        low = sorted(
            p for p, c in proposals.confidences.items() if c < bundle.thresholds.low_confidence
        )
        flags = []
        if seriousness.serious:
            flags.append(cases.SERIOUS_BY_SIGNALS)
        if seriousness.disagreements:
            flags.append("seriousness_disagreement")
        if duplicates:
            flags.append("duplicate_candidates")
        if low:
            flags.append("low_confidence")
        if proposals.rejections:
            flags.append("rejected_values")
        if not inp.ai_available:
            flags.append("ai_unavailable")
        if source is None:
            flags.append("no_source_text")
        if decision.level is Level.L2:
            flags.append("straight_through")
        return {
            "proposals": proposals,
            "seriousness": seriousness,
            "coding": coding,
            "questions": questions,
            "duplicates": duplicates,
            "decision": decision,
            "low_confidence": low,
            "releases": {k.value: self._d.dictionary.release(k) for k in Kind},
            "flags": flags,
        }

    # ------------------------------------------------------------ narrative

    @activity.defn(name="draft_narrative")
    def draft_narrative(self, inp: NarrativeInput) -> str:
        """A narrative draft from confirmed fields only, kept as a suggestion."""
        tenant = UUID(inp.tenant_id)
        with self._d.database.tenant_session(tenant) as session:
            case = session.get_one(CaseRow, UUID(inp.case_id))
            if case_ai.run_of(session, case.id, inp.version, case_ai.NARRATIVE) is not None:
                return "exists"
            content = cases.case_of(cases.get_version(session, case.id, inp.version))
        facts = confirmed_facts(content)
        if not facts:
            return "no_confirmed_facts"
        redaction = redact("\n".join(facts), case_ai.personal_values(content))
        step = StepInput(
            inp.tenant_id, inp.case_id, inp.version, self._d.bundle_version,
            {"facts": redaction.text},
        )  # fmt: skip
        output = NarrativeOut.model_validate(self._call(Step.NARRATIVE, step))
        unknown = set(TOKEN.findall(output.narrative)) - set(redaction.tokens)
        if unknown:  # the model cited a person it was never given
            return "rejected"
        with self._d.database.tenant_session(tenant) as session:
            case = session.get_one(CaseRow, UUID(inp.case_id))
            case_ai.record_run(
                session, case, inp.version, kind=case_ai.NARRATIVE,
                bundle_version=self._d.bundle_version, status="completed", level=Level.L1,
                flags=[], result={"draft": redaction.restore(output.narrative),
                                  "facts_used": len(facts)},
                now=self._d.clock(),
            )  # fmt: skip
        return "drafted"

    def all(self) -> list[Callable[..., Any]]:
        return [
            self.prepare,
            self.extract,
            self.plan,
            self.assess_seriousness,
            self.code_terms,
            self.ask_follow_up,
            self.finalize,
            self.draft_narrative,
        ]


def confirmed_facts(case: SafetyCase) -> list[str]:
    """``path: value`` for every confirmed field (unconfirmed values are left out)."""
    facts: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            if "source" in node and "value" in node:
                if node.get("confirmed_by"):
                    value = node["value"]
                    shown = json.dumps(value) if isinstance(value, dict) else str(value)
                    facts.append(f"{path}: {shown}")
                return
            for key, child in node.items():
                walk(child, f"{path}/{key}")
        elif isinstance(node, list):
            for item in node:
                walk(item, f"{path}/{item.get('id', '?') if isinstance(item, dict) else '?'}")

    walk(case.to_json(), "")
    return facts


def _result(outcome: dict[str, Any], applied: int, inp: FinalizeInput) -> dict[str, Any]:
    proposals: Proposals = outcome["proposals"]
    decision = outcome["decision"]
    seriousness = outcome["seriousness"]
    return {
        "automation": {"level": decision.level.value, "reasons": list(decision.reasons)},
        "ai_available": inp.ai_available,
        "unavailable_reason": inp.unavailable_reason,
        "applied_changes": applied,
        # At L0 these are suggestions for the side panel; at L1/L2 they are in the case.
        "proposals": [c.model_dump(mode="json") for c in proposals.changes],
        "confidences": proposals.confidences,
        "low_confidence": outcome["low_confidence"],
        "rejections": [r.__dict__ for r in proposals.rejections],
        "seriousness": {
            "llm": seriousness.llm,
            "rules": seriousness.rules,
            "met": seriousness.met,
            "disagreements": seriousness.disagreements,
        },
        "coding": outcome["coding"],
        "dictionary_releases": outcome["releases"],
        "duplicates": [d.__dict__ | {"reasons": list(d.reasons)} for d in outcome["duplicates"]],
        "follow_up_questions": outcome["questions"],
    }


def _summary(row: Any, applied: int) -> PipelineSummary:
    return PipelineSummary(
        str(row.case_id), row.case_version, row.bundle_version, row.status,
        row.automation_level, list(row.flags), applied,
    )  # fmt: skip
