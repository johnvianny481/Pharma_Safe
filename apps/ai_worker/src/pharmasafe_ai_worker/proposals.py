"""From model output to case proposals, under the evidence rule.

Every value the model proposes becomes a ``Tracked`` value with ``source: ai``, an
evidence span and a confidence, or it is rejected with a reason:

- the quote must be found verbatim in the text the model was given;
- for free-text values (names, terms, product names, numbers) the quote must contain
  the value, so a real quote cannot be attached to an invented value;
- coded values (sex, outcome, qualification, dates, units) must be valid for the case
  model;
- confidence must be between 0 and 1.

Evidence found in the redacted text is mapped back to the original document offsets,
so a reviewer sees exactly where each value came from. Proposals only ever fill empty
fields: a value a person entered is never replaced by the AI.

The functions are pure. :func:`build_extraction` runs twice: in the *plan* activity on
redacted values (whose output is stored in workflow history) and in the *finalize*
activity with token restoration (whose output goes to the case only). Both runs give the
same items and ids.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from pharmasafe_ai_gateway.schemas import (
    CodingOut,
    DrugOut,
    ExtractionOut,
    FollowUpOut,
    NumberValue,
    ReactionOut,
    ReporterOut,
    SeriousnessOut,
    TextValue,
)
from pharmasafe_ai_worker.dictionary import Dictionary, Kind
from pharmasafe_domain.ai.duplicates import normalise_term
from pharmasafe_domain.ai.evidence import Rejection, check_evidence
from pharmasafe_domain.ai.redaction import Redaction, find_identifiers
from pharmasafe_domain.ai.seriousness import RuleHit, combine
from pharmasafe_domain.case import (
    AgeUnit,
    DrugRole,
    Outcome,
    Qualification,
    SafetyCase,
    Sex,
)
from pharmasafe_domain.changes import AddItem, ChangeError, SetField, apply_change
from pharmasafe_domain.dates import date_bounds
from pharmasafe_domain.rules.seriousness import CRITERIA

type Change = SetField | AddItem

_AGE_UNITS = {
    "a": AgeUnit.YEAR, "year": AgeUnit.YEAR, "years": AgeUnit.YEAR,
    "mo": AgeUnit.MONTH, "month": AgeUnit.MONTH, "months": AgeUnit.MONTH,
    "wk": AgeUnit.WEEK, "week": AgeUnit.WEEK, "weeks": AgeUnit.WEEK,
    "d": AgeUnit.DAY, "day": AgeUnit.DAY, "days": AgeUnit.DAY,
    "h": AgeUnit.HOUR, "hour": AgeUnit.HOUR, "hours": AgeUnit.HOUR,
    "{decade}": AgeUnit.DECADE,
}  # fmt: skip
_COUNTRY = re.compile(r"^[A-Z]{2}$")


class EvidenceMapper:
    """Turns a model's (value, quote, confidence) into a Tracked value, or a rejection."""

    def __init__(
        self,
        redaction: Redaction,
        *,
        document_id: str,
        original: str,
        offset: int,
        restore: bool,
    ) -> None:
        self.redaction = redaction
        self._document_id = document_id
        self._original = original
        self._offset = offset
        self._restore = restore

    @classmethod
    def redacted_only(cls, redacted_text: str, document_id: str) -> "EvidenceMapper":
        """For planning on redacted text: evidence stays in redacted coordinates."""
        return cls(
            Redaction(redacted_text),
            document_id=document_id,
            original=redacted_text,
            offset=0,
            restore=False,
        )

    def value(self, text: str) -> str:
        return self.redaction.restore(text) if self._restore else text

    def tracked(
        self,
        path: str,
        value: Any,
        quote: str | None,
        confidence: float,
        *,
        must_contain: str | None = None,
    ) -> dict[str, Any] | Rejection:
        if not 0.0 <= confidence <= 1.0:
            return Rejection(path, "confidence outside 0..1")
        found = check_evidence(path, quote, self.redaction.text, value=must_contain)
        if isinstance(found, Rejection):
            return found
        start, end = self.redaction.to_original(found.start, found.end)
        return {
            "value": value,
            "source": "ai",
            "evidence": {
                "document_id": self._document_id,
                "start": self._offset + start,
                "end": self._offset + end,
                "text": self._original[start:end],
            },
            "confidence": confidence,
        }


@dataclass
class Proposals:
    case: SafetyCase
    changes: list[Change] = field(default_factory=list)
    confidences: dict[str, float] = field(default_factory=dict)
    rejections: list[Rejection] = field(default_factory=list)
    # The evidence of each AI-proposed reaction term, to evidence its MedDRA code.
    term_evidence: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add(self, change: Change, confidences: dict[str, float]) -> bool:
        try:
            self.case, _ = apply_change(self.case, change)
        except ChangeError as exc:
            self.rejections.append(Rejection(change.path, f"not valid for the case: {exc}"))
            return False
        self.changes.append(change)
        self.confidences.update(confidences)
        return True


def _number(value: float) -> str:
    return format(Decimal(str(value)).normalize(), "f")


class _Fields:
    """Collects the accepted fields of one section (patient, one drug, ...)."""

    def __init__(self, mapper: EvidenceMapper, proposals: Proposals, base: str) -> None:
        self._mapper = mapper
        self._proposals = proposals
        self._base = base
        self.values: dict[str, dict[str, Any]] = {}
        self.confidences: dict[str, float] = {}

    def text(self, name: str, out: TextValue | None, *, enum: type[Any] | None = None) -> None:
        if out is None:
            return
        path = f"{self._base}/{name}"
        value: Any = self._mapper.value(out.value).strip()
        if enum is not None:
            allowed = {member.value for member in enum}
            value = value.lower()
            if value not in allowed:
                self._proposals.rejections.append(Rejection(path, f"{value!r} is not a valid code"))
                return
            must_contain = None
        else:
            must_contain = out.value
        self._put(path, name, value, out.quote, out.confidence, must_contain)

    def date(self, name: str, out: TextValue | None) -> None:
        if out is None:
            return
        path = f"{self._base}/{name}"
        try:
            date_bounds(out.value)
        except ValueError:
            self._proposals.rejections.append(Rejection(path, "not a YYYY[-MM[-DD]] date"))
            return
        self._put(path, name, out.value, out.quote, out.confidence, None)

    def country(self, name: str, out: TextValue | None) -> None:
        if out is None:
            return
        value = out.value.strip().upper()
        if not _COUNTRY.match(value):
            path = f"{self._base}/{name}"
            self._proposals.rejections.append(Rejection(path, "not an ISO 3166 country code"))
            return
        self._put(f"{self._base}/{name}", name, value, out.quote, out.confidence, None)

    def number(self, name: str, out: NumberValue | None) -> None:
        if out is None:
            return
        path = f"{self._base}/{name}"
        if out.value < 0:
            self._proposals.rejections.append(Rejection(path, "negative value"))
            return
        number = _number(out.value)
        self._put(path, name, number, out.quote, out.confidence, number)

    def age(self, out: NumberValue | None) -> None:
        if out is None:
            return
        path = f"{self._base}/age"
        unit = _AGE_UNITS.get((out.unit or "").strip().lower())
        if unit is None or out.value < 0:
            self._proposals.rejections.append(Rejection(path, "not a valid age and unit"))
            return
        number = _number(out.value)
        value = {"value": number, "unit": unit.value}
        self._put(path, "age", value, out.quote, out.confidence, number)

    def dose(self, out: NumberValue | None) -> None:
        if out is None:
            return
        path = f"{self._base}/dose"
        unit = (out.unit or "").strip()
        if not unit or len(unit) > 50 or out.value < 0:
            self._proposals.rejections.append(Rejection(path, "not a valid dose and unit"))
            return
        number = _number(out.value)
        self._put(path, "dose", {"value": number, "unit": unit}, out.quote, out.confidence, number)

    def _put(
        self,
        path: str,
        name: str,
        value: Any,
        quote: str,
        confidence: float,
        must_contain: str | None,
    ) -> None:
        tracked = self._mapper.tracked(path, value, quote, confidence, must_contain=must_contain)
        if isinstance(tracked, Rejection):
            self._proposals.rejections.append(tracked)
            return
        self.values[name] = tracked
        self.confidences[path] = confidence


def _fill(proposals: Proposals, section: Any, fields: _Fields, base: str) -> None:
    """SetField for each accepted value whose field is still empty."""
    for name, tracked in fields.values.items():
        if getattr(section, name) is None:
            path = f"{base}/{name}"
            proposals.add(SetField(path=path, value=tracked), {path: fields.confidences[path]})


def _next_id(prefix: str, existing: set[str]) -> str:
    n = 1
    while f"{prefix}{n}" in existing:
        n += 1
    existing.add(f"{prefix}{n}")
    return f"{prefix}{n}"


def build_extraction(
    current: SafetyCase, extraction: dict[str, Any], mapper: EvidenceMapper
) -> Proposals:
    proposals = Proposals(case=current)
    try:
        out = ExtractionOut.model_validate(extraction)
    except ValidationError:
        proposals.rejections.append(Rejection("/", "extraction output does not match the schema"))
        return proposals

    patient = _Fields(mapper, proposals, "/patient")
    patient.text("initials", out.patient.initials)
    patient.text("sex", out.patient.sex, enum=Sex)
    patient.age(out.patient.age)
    patient.number("weight_kg", out.patient.weight_kg)
    _fill(proposals, current.patient, patient, "/patient")

    if out.occurrence_country is not None:
        admin = _Fields(mapper, proposals, "/admin")
        admin.country("occurrence_country", out.occurrence_country)
        _fill(proposals, current.admin, admin, "/admin")

    _reporters(proposals, mapper, out.reporters)
    _drugs(proposals, mapper, out.drugs)
    _reactions(proposals, mapper, out.reactions)
    return proposals


def _reporter_fields(
    mapper: EvidenceMapper, proposals: Proposals, base: str, r: ReporterOut
) -> _Fields:
    fields = _Fields(mapper, proposals, base)
    fields.text("qualification", r.qualification, enum=Qualification)
    fields.text("given_name", r.given_name)
    fields.text("family_name", r.family_name)
    fields.country("country", r.country)
    return fields


def _reporters(proposals: Proposals, mapper: EvidenceMapper, reporters: list[ReporterOut]) -> None:
    if not reporters:
        return
    existing = proposals.case.reporters
    if existing:  # fill the first reporter's gaps; a person already identified them
        first = existing[0]
        base = f"/reporters/{first.id}"
        _fill(proposals, first, _reporter_fields(mapper, proposals, base, reporters[0]), base)
        return
    ids = {r.id for r in existing}
    for reporter in reporters:
        item_id = _next_id("ai-rp", ids)
        fields = _reporter_fields(mapper, proposals, f"/reporters/{item_id}", reporter)
        if fields.values:
            proposals.add(AddItem(path="/reporters", item={"id": item_id, **fields.values}),
                          fields.confidences)  # fmt: skip


def _drugs(proposals: Proposals, mapper: EvidenceMapper, drugs: list[DrugOut]) -> None:
    ids = {d.id for d in proposals.case.drugs}
    for drug in drugs:
        name = normalise_term(mapper.value(drug.product_name.value))
        match = next(
            (
                d for d in proposals.case.drugs
                for n in (d.product_name, d.active_substance)
                if n is not None and normalise_term(n.value) == name
            ),
            None,
        )  # fmt: skip
        item_id = match.id if match else _next_id("ai-d", ids)
        base = f"/drugs/{item_id}"
        fields = _Fields(mapper, proposals, base)
        fields.text("product_name", drug.product_name)
        fields.text("role", drug.role, enum=DrugRole)
        fields.dose(drug.dose)
        fields.text("route", drug.route)
        fields.date("start_date", drug.start_date)
        fields.text("indication", drug.indication)
        if match is not None:
            _fill(proposals, match, fields, base)
        elif "product_name" in fields.values:
            proposals.add(AddItem(path="/drugs", item={"id": item_id, **fields.values}),
                          fields.confidences)  # fmt: skip
        else:
            ids.discard(item_id)


def _reactions(proposals: Proposals, mapper: EvidenceMapper, reactions: list[ReactionOut]) -> None:
    ids = {r.id for r in proposals.case.reactions}
    for reaction in reactions:
        term = normalise_term(mapper.value(reaction.reported_term.value))
        match = next(
            (r for r in proposals.case.reactions
             if r.reported_term is not None and normalise_term(r.reported_term.value) == term),
            None,
        )  # fmt: skip
        item_id = match.id if match else _next_id("ai-r", ids)
        base = f"/reactions/{item_id}"
        fields = _Fields(mapper, proposals, base)
        fields.text("reported_term", reaction.reported_term)
        fields.date("onset_date", reaction.onset_date)
        fields.text("outcome", reaction.outcome, enum=Outcome)
        if match is not None:
            _fill(proposals, match, fields, base)
        elif "reported_term" in fields.values:
            if proposals.add(AddItem(path="/reactions", item={"id": item_id, **fields.values}),
                             fields.confidences):  # fmt: skip
                proposals.term_evidence[item_id] = fields.values["reported_term"]
        else:
            ids.discard(item_id)


# ---------------------------------------------------------------- seriousness


@dataclass(frozen=True)
class SeriousnessResult:
    llm: dict[str, bool]
    rules: dict[str, bool]
    met: dict[str, bool]
    disagreements: list[str]

    @property
    def serious(self) -> bool:
        return any(self.met.values())


def apply_seriousness(
    proposals: Proposals,
    output: dict[str, Any] | None,
    rules: dict[str, RuleHit | None],
    mapper: EvidenceMapper,
) -> SeriousnessResult:
    """Combine both signals and propose the criteria that are met (never "not met").

    A criterion is proposed as met with the LLM's evidence when its quote checks out,
    otherwise with the rule's evidence. "Not met" is never proposed: a person must assess
    it, because the absence of a mention is not evidence.
    """
    per_reaction: dict[str, dict[str, Any]] = {}
    if output is not None:
        try:
            parsed = SeriousnessOut.model_validate(output)
            per_reaction = {r.reaction_id: r.criteria.model_dump() for r in parsed.reactions}
        except ValidationError:
            proposals.rejections.append(Rejection("/reactions", "seriousness output is invalid"))
    reactions = proposals.case.reactions
    llm_any = {c: any(per_reaction.get(r.id, {}).get(c, {}).get("met") for r in reactions)
               for c in CRITERIA}  # fmt: skip
    combined = {c.criterion: c for c in combine(llm_any, rules)}

    for criterion in CRITERIA:
        if not combined[criterion].met or not reactions:
            continue
        hit = rules.get(criterion)
        written = False
        for reaction in reactions:
            llm = per_reaction.get(reaction.id, {}).get(criterion) or {}
            if not llm.get("met") or getattr(reaction.seriousness, criterion) is not None:
                continue
            path = f"/reactions/{reaction.id}/seriousness/{criterion}"
            tracked = mapper.tracked(path, True, llm.get("quote"), float(llm["confidence"]))
            if isinstance(tracked, Rejection) and hit is not None:
                tracked = mapper.tracked(path, True, hit.text, 0.6)
            if isinstance(tracked, Rejection):
                proposals.rejections.append(tracked)
                continue
            written = (
                proposals.add(SetField(path=path, value=tracked), {path: tracked["confidence"]})
                or written
            )
        first = reactions[0]
        if not written and hit is not None and getattr(first.seriousness, criterion) is None:
            # Only the rules saw it (or the model's evidence failed): propose it on the
            # first reaction with the rule's evidence, at a modest confidence.
            path = f"/reactions/{first.id}/seriousness/{criterion}"
            tracked = mapper.tracked(path, True, hit.text, 0.6)
            if isinstance(tracked, Rejection):
                proposals.rejections.append(tracked)
            else:
                proposals.add(SetField(path=path, value=tracked), {path: 0.6})

    return SeriousnessResult(
        llm=llm_any,
        rules={c: rules.get(c) is not None for c in CRITERIA},
        met={c: combined[c].met for c in CRITERIA},
        disagreements=[c for c in CRITERIA if combined[c].disagreement],
    )


# ---------------------------------------------------------------- coding


def coding_items(case: SafetyCase, dictionary: Dictionary, limit: int) -> list[dict[str, Any]]:
    """Reactions without a MedDRA code and drugs, each with dictionary candidates."""
    wanted: list[tuple[str, Kind, str]] = [
        (f"r:{r.id}", Kind.MEDDRA_LLT, r.reported_term.value)
        for r in case.reactions
        if r.reported_term is not None and r.meddra_code is None
    ]
    for drug in case.drugs:
        name = drug.product_name or drug.active_substance
        if name is not None:
            wanted.append((f"d:{drug.id}", Kind.WHODRUG, name.value))
    items = []
    for item_id, kind, term in wanted:
        candidates = dictionary.search(kind, term, limit)
        if candidates:
            listed = [{"id": c.code, "label": c.label} for c in candidates]
            items.append({"id": item_id, "kind": kind.value, "term": term, "candidates": listed})
    return items


def apply_coding(
    proposals: Proposals, output: dict[str, Any] | None, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Accept only codes that were among the candidates; MedDRA codes go into the case."""
    if output is None:
        return []
    try:
        parsed = CodingOut.model_validate(output)
    except ValidationError:
        proposals.rejections.append(Rejection("/", "coding output is invalid"))
        return []
    by_id = {item["id"]: item for item in items}
    suggestions = []
    for choice in parsed.choices:
        item = by_id.get(choice.item_id)
        if item is None or choice.candidate_id is None:
            continue
        labels = {c["id"]: c["label"] for c in item["candidates"]}
        path = f"/{'reactions' if item['kind'] == 'meddra_llt' else 'drugs'}/{choice.item_id[2:]}"
        if choice.candidate_id not in labels:
            proposals.rejections.append(
                Rejection(path, "code is not one of the dictionary candidates")
            )
            continue
        if not 0.0 <= choice.confidence <= 1.0:
            proposals.rejections.append(Rejection(path, "confidence outside 0..1"))
            continue
        suggestions.append({"item": choice.item_id, "kind": item["kind"], "term": item["term"],
                            "code": choice.candidate_id, "label": labels[choice.candidate_id],
                            "confidence": choice.confidence})  # fmt: skip
        evidence = proposals.term_evidence.get(choice.item_id[2:])
        if item["kind"] == "meddra_llt" and evidence is not None:
            code_path = f"{path}/meddra_code"
            tracked = {**evidence, "value": choice.candidate_id, "confidence": choice.confidence}
            proposals.add(SetField(path=code_path, value=tracked), {code_path: choice.confidence})
    return suggestions


# ---------------------------------------------------------------- follow-up


def accept_questions(
    proposals: Proposals, output: dict[str, Any] | None, gaps: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Questions only about the gaps given, and free of personal data."""
    if output is None:
        return []
    try:
        parsed = FollowUpOut.model_validate(output)
    except ValidationError:
        proposals.rejections.append(Rejection("/", "follow-up output is invalid"))
        return []
    allowed = {gap["path"] for gap in gaps}
    accepted = []
    for question in parsed.questions:
        text = question.question.strip()
        if question.path not in allowed:
            proposals.rejections.append(Rejection(question.path, "question about a gap not given"))
        elif not text or len(text) > 500 or find_identifiers(text):
            proposals.rejections.append(Rejection(question.path, "question text not acceptable"))
        else:
            accepted.append({"path": question.path, "question": text})
    return accepted
