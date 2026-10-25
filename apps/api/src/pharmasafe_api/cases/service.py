"""The case store: versions, changes with provenance, approval, follow-ups and rebuilds.

All rules live in :mod:`pharmasafe_domain`. This module persists their results and keeps
four things in one transaction for every operation:

1. the new case state (``cases``, ``case_versions``),
2. one audit ledger entry per change, with old and new value, reason and case version,
3. any regulatory clocks the new state starts (``case_clocks``, never reset),
4. outbox events for other services.

**Rebuilding.** Each version starts with a ``case.version_created`` audit entry holding
its base content; every later change is its own entry. Replaying those entries with the
same domain function that applied them (:func:`pharmasafe_domain.changes.replay`)
rebuilds the version as it was at any moment. For the current state, the rebuild's hash
must equal the stored ``content_hash``; :func:`rebuild_version` checks that.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, func, select, text, true
from sqlalchemy.orm import Session

from pharmasafe_api.cases.models import CaseClockRow, CaseRow, CaseVersionRow
from pharmasafe_domain.case import DrugRole, SafetyCase, normalise
from pharmasafe_domain.changes import AddItem, ChangeError, RemoveItem, SetField, apply_change
from pharmasafe_domain.provenance import Tracked
from pharmasafe_domain.regions import region_for_country
from pharmasafe_domain.rules.checks import Issue, approval_blockers
from pharmasafe_domain.rules.clocks import ClockTracker, Obligation, RuleSetRegistry, Snapshot
from pharmasafe_domain.rules.clocks import day_zero as first_day_zero
from pharmasafe_domain.rules.review import (
    ReviewPlan,
    SignatureFact,
    Step,
    qc_sampled,
    review_plan,
    signing_blockers,
)
from pharmasafe_platform.audit import Actor, AuditEvent, append_audit
from pharmasafe_platform.auth import DataScope, Principal
from pharmasafe_platform.models import AuditLedgerRow, ESignatureRow, TenantRow
from pharmasafe_platform.outbox import OutboxEvent, enqueue
from pharmasafe_platform.signatures import (
    SignableRecord,
    SignatureMeaning,
    SignatureService,
    content_hash,
)

ENTITY = "case"
TOPIC = "cases"
DRAFT = "draft"
APPROVED = "approved"

VERSION_CREATED = "case.version_created"
CHANGE_ACTIONS = {"set": "case.field_set", "add": "case.item_added", "remove": "case.item_removed"}

type Change = SetField | AddItem | RemoveItem


class CaseError(Exception):
    pass


class CaseNotFound(CaseError):
    pass


class CaseConflict(CaseError):
    """The request does not fit the case's current state (stale revision, approved, ...)."""


class InvalidChange(CaseError):
    pass


class ApprovalBlocked(CaseError):
    def __init__(self, issues: Sequence[Issue]) -> None:
        super().__init__(f"{len(issues)} issue(s) block approval")
        self.issues = list(issues)


@dataclass(frozen=True)
class CaseContext:
    """Everything an operation needs besides the session."""

    registry: RuleSetRegistry
    now: datetime

    @property
    def today(self) -> date:
        return self.now.astimezone(UTC).date()


# ---------------------------------------------------------------- reading


def scope_filter(scope: DataScope) -> ColumnElement[bool]:
    """Restrict queries to the products and regions the user may see (fail closed)."""
    condition: ColumnElement[bool] = true()
    if scope.regions is not None:
        condition = condition & CaseRow.region.in_(sorted(r.lower() for r in scope.regions))
    if scope.products is not None:
        condition = condition & CaseRow.product_codes.overlap(sorted(scope.products))
    return condition


def list_cases(session: Session, scope: DataScope, *, limit: int = 100) -> Sequence[CaseRow]:
    query = select(CaseRow).where(scope_filter(scope)).order_by(CaseRow.seq.desc()).limit(limit)
    return session.scalars(query).all()


def get_case(session: Session, case_id: UUID, scope: DataScope) -> CaseRow:
    """A case the caller may see. Out-of-scope and other tenants' cases look the same: absent."""
    row = session.scalars(
        select(CaseRow).where(CaseRow.id == case_id, scope_filter(scope))
    ).one_or_none()
    if row is None:
        raise CaseNotFound(f"case {case_id} not found")
    return row


def list_versions(session: Session, case_id: UUID) -> Sequence[CaseVersionRow]:
    return session.scalars(
        select(CaseVersionRow)
        .where(CaseVersionRow.case_id == case_id)
        .order_by(CaseVersionRow.version)
    ).all()


def get_version(
    session: Session, case_id: UUID, version: int, *, for_update: bool = False
) -> CaseVersionRow:
    query = select(CaseVersionRow).where(
        CaseVersionRow.case_id == case_id, CaseVersionRow.version == version
    )
    row = session.scalars(query.with_for_update() if for_update else query).one_or_none()
    if row is None:
        raise CaseNotFound(f"case {case_id} has no version {version}")
    return row


def list_clocks(session: Session, case_id: UUID) -> Sequence[CaseClockRow]:
    return session.scalars(
        select(CaseClockRow)
        .where(CaseClockRow.case_id == case_id)
        .order_by(CaseClockRow.due_date, CaseClockRow.regulator, CaseClockRow.rule_id)
    ).all()


def case_of(version: CaseVersionRow) -> SafetyCase:
    return SafetyCase.from_json(version.content)


# ---------------------------------------------------------------- writing


def create_case(
    session: Session,
    tenant_id: UUID,
    actor: Actor,
    content: SafetyCase,
    ctx: CaseContext,
    *,
    reason: str | None = None,
) -> tuple[CaseRow, CaseVersionRow]:
    """A new case with ``content`` as version 1 (a draft). ``actor`` may be a system channel."""
    # Serialise numbering per tenant, like the audit ledger's sequence.
    session.execute(
        text("select pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"case-seq:{tenant_id}"},
    )
    seq = (session.scalar(select(func.max(CaseRow.seq))) or 0) + 1
    case = CaseRow(
        id=uuid4(),
        tenant_id=tenant_id,
        seq=seq,
        case_number=f"PS-{seq:06d}",
        product_codes=[],
        region=region_for_country(None),
        latest_version=1,
        created_at=ctx.now,
        created_by=actor.id,
    )
    session.add(case)
    session.flush()
    _audit(
        session,
        case,
        actor,
        ctx,
        action="case.created",
        version=1,
        new_value={"case_number": case.case_number},
        reason=reason,
    )
    version = _new_version(session, case, 1, content, actor, ctx, reason)
    _enqueue(session, case, "case.created", ctx, version=1)
    return case, version


def apply_changes(
    session: Session,
    case: CaseRow,
    version_number: int,
    *,
    expected_revision: int,
    changes: Sequence[Change],
    principal: Principal,
    ctx: CaseContext,
    reason: str | None = None,
) -> CaseVersionRow:
    """Apply changes to a draft, all or nothing, if nobody else changed it meanwhile."""
    version = get_version(session, case.id, version_number, for_update=True)
    if version.status != DRAFT:
        raise CaseConflict("approved versions are immutable; start a follow-up to change the case")
    if version.revision != expected_revision:
        raise CaseConflict(
            f"version {version_number} is at revision {version.revision}, not {expected_revision}"
        )
    actor = Actor.from_principal(principal)
    current = case_of(version)
    for change in changes:
        try:
            current, old = apply_change(current, change)
        except ChangeError as exc:
            raise InvalidChange(str(exc)) from exc
        new = (
            change.value
            if isinstance(change, SetField)
            else (change.item if isinstance(change, AddItem) else None)
        )
        _audit(
            session,
            case,
            actor,
            ctx,
            action=CHANGE_ACTIONS[change.op],
            version=version_number,
            field=change.path,
            old_value=old,
            new_value=new,
            reason=reason,
        )
    _store(session, case, version, current, actor, ctx)
    _enqueue(session, case, "case.updated", ctx, version=version_number, revision=version.revision)
    return version


def approve_version(
    session: Session,
    case: CaseRow,
    version_number: int,
    *,
    signer: Principal,
    reauth: Principal,
    signatures: SignatureService,
    ctx: CaseContext,
    reason: str | None = None,
) -> CaseVersionRow:
    """Lock a draft as an approved version with an e-signature bound to its content."""
    version = get_version(session, case.id, version_number, for_update=True)
    if version.status != DRAFT:
        raise CaseConflict(f"version {version_number} is already approved")
    blockers = approval_blockers(case_of(version), today=ctx.today)
    blockers += review_blockers(session, case, version, Step.APPROVAL, signer.subject)
    if blockers:
        raise ApprovalBlocked(blockers)
    signature = signatures.sign(
        session,
        signer=signer,
        reauth=reauth,
        record=SignableRecord(ENTITY, str(case.id), version_number, version.content_hash),
        meaning=SignatureMeaning.APPROVED,
        reason=reason,
        now=ctx.now,
    )
    actor = Actor.from_principal(signer)
    version.status = APPROVED
    version.approved_at = ctx.now
    version.approved_by = actor.id
    version.signature_id = signature.id
    version.revision += 1
    session.flush()
    _audit(
        session,
        case,
        actor,
        ctx,
        action="case.version_approved",
        version=version_number,
        new_value={"content_hash": version.content_hash, "signature_id": str(signature.id)},
        reason=reason,
    )
    _enqueue(session, case, "case.version_approved", ctx, version=version_number)
    return version


def sign_review(
    session: Session,
    case: CaseRow,
    version_number: int,
    *,
    step: Step,
    signer: Principal,
    reauth: Principal,
    signatures: SignatureService,
    ctx: CaseContext,
    reason: str | None = None,
) -> ESignatureRow:
    """Sign a review step (data entry, quality or medical) on the version's current content."""
    if step is Step.APPROVAL:
        raise ValueError("approval is signed through approve_version")
    version = get_version(session, case.id, version_number, for_update=True)
    if version.status != DRAFT:
        raise CaseConflict(f"version {version_number} is already approved")
    blockers = review_blockers(session, case, version, step, signer.subject)
    if blockers:
        raise ApprovalBlocked(blockers)
    return signatures.sign(
        session,
        signer=signer,
        reauth=reauth,
        record=SignableRecord(ENTITY, str(case.id), version_number, version.content_hash),
        meaning=SignatureMeaning(step.value),
        reason=reason,
        now=ctx.now,
    )


def review_plan_of(session: Session, case: CaseRow, version: CaseVersionRow) -> ReviewPlan:
    tenant = session.get_one(TenantRow, case.tenant_id)
    sampled = qc_sampled(str(case.id), version.version, tenant.qc_sample_rate)
    return review_plan(case_of(version), sampled=sampled)


def review_signatures(session: Session, case_id: UUID, version: int) -> list[SignatureFact]:
    rows = session.scalars(
        select(ESignatureRow).where(
            ESignatureRow.entity_type == ENTITY,
            ESignatureRow.entity_id == str(case_id),
            ESignatureRow.entity_version == version,
        )
    )
    return [SignatureFact(r.meaning, r.signer_id, r.content_hash) for r in rows]


def review_blockers(
    session: Session, case: CaseRow, version: CaseVersionRow, step: Step, signer_id: str
) -> list[Issue]:
    return signing_blockers(
        step,
        signer_id,
        review_signatures(session, case.id, version.version),
        review_plan_of(session, case, version),
        version.content_hash,
    )


def start_follow_up(
    session: Session,
    case: CaseRow,
    *,
    receipt_date: date,
    principal: Principal,
    ctx: CaseContext,
    reason: str | None = None,
) -> CaseVersionRow:
    """New information on an approved case: a new draft version, based on the latest one.

    The follow-up's receipt date is recorded as the new version's first change, so any
    clock the follow-up starts runs from the date the new information arrived.
    """
    latest = get_version(session, case.id, case.latest_version, for_update=True)
    if latest.status != APPROVED:
        raise CaseConflict(f"version {latest.version} is still a draft; change it instead")
    actor = Actor.from_principal(principal)
    number = latest.version + 1
    case.latest_version = number
    # Derived state (clocks above all) waits for the new receipt date, set just below.
    version = _new_version(session, case, number, case_of(latest), actor, ctx, reason, derive=False)
    receipt = Tracked[date].human(receipt_date, by=actor.id, at=ctx.now)
    change = SetField(path="/admin/latest_receipt_date", value=receipt.model_dump(mode="json"))
    apply_changes(
        session,
        case,
        number,
        expected_revision=version.revision,
        changes=[change],
        principal=principal,
        ctx=ctx,
        reason=reason,
    )
    _enqueue(session, case, "case.follow_up_started", ctx, version=number)
    return version


# ---------------------------------------------------------------- rebuilding


@dataclass(frozen=True)
class Rebuild:
    case: SafetyCase
    changes_applied: int
    # Whether the rebuild equals the stored version. Only meaningful without ``as_of``.
    matches_stored: bool | None


def rebuild_version(
    session: Session, case: CaseRow, version_number: int, *, as_of: datetime | None = None
) -> Rebuild:
    """Rebuild a version from the audit ledger, as it is now or as it was at ``as_of``."""
    stored = get_version(session, case.id, version_number)
    query = (
        select(AuditLedgerRow)
        .where(
            AuditLedgerRow.entity_type == ENTITY,
            AuditLedgerRow.entity_id == str(case.id),
            AuditLedgerRow.case_version == version_number,
            AuditLedgerRow.action.in_([VERSION_CREATED, *CHANGE_ACTIONS.values()]),
        )
        .order_by(AuditLedgerRow.seq)
    )
    if as_of is not None:
        query = query.where(AuditLedgerRow.occurred_at <= as_of)
    entries = session.scalars(query).all()
    if not entries or entries[0].action != VERSION_CREATED:
        raise CaseNotFound(f"version {version_number} did not exist at {as_of}")

    current = SafetyCase.from_json(entries[0].new_value)
    for entry in entries[1:]:
        current, _ = apply_change(current, _change_from(entry))
    matches = None
    if as_of is None:
        matches = content_hash(current.to_json()) == stored.content_hash
    return Rebuild(case=current, changes_applied=len(entries) - 1, matches_stored=matches)


def _change_from(entry: AuditLedgerRow) -> Change:
    path = entry.field or ""
    if entry.action == CHANGE_ACTIONS["set"]:
        return SetField(path=path, value=entry.new_value)
    if entry.action == CHANGE_ACTIONS["add"]:
        return AddItem(path=path, item=entry.new_value)
    return RemoveItem(path=path)


# ---------------------------------------------------------------- internals


def _new_version(
    session: Session,
    case: CaseRow,
    number: int,
    content: SafetyCase,
    actor: Actor,
    ctx: CaseContext,
    reason: str | None,
    *,
    derive: bool = True,
) -> CaseVersionRow:
    data = normalise(content.to_json())
    version = CaseVersionRow(
        case_id=case.id,
        version=number,
        tenant_id=case.tenant_id,
        status=DRAFT,
        schema_version=content.schema_version,
        content=data,
        content_hash=content_hash(data),
        revision=0,
        created_at=ctx.now,
        created_by=actor.id,
    )
    session.add(version)
    session.flush()
    _audit(
        session,
        case,
        actor,
        ctx,
        action=VERSION_CREATED,
        version=number,
        new_value=data,
        reason=reason,
    )
    if derive:
        _after_change(session, case, number, content, actor, ctx)
    return version


def _store(
    session: Session,
    case: CaseRow,
    version: CaseVersionRow,
    content: SafetyCase,
    actor: Actor,
    ctx: CaseContext,
) -> None:
    data = normalise(content.to_json())
    version.content = data
    version.content_hash = content_hash(data)
    version.revision += 1
    session.flush()
    _after_change(session, case, version.version, content, actor, ctx)


def _after_change(
    session: Session,
    case: CaseRow,
    version: int,
    content: SafetyCase,
    actor: Actor,
    ctx: CaseContext,
) -> None:
    """Derived state: scope columns, day 0 and regulatory clocks."""
    if version == case.latest_version:
        country = content.admin.occurrence_country
        case.region = region_for_country(country.value if country else None)
        case.product_codes = _product_codes(content)

    snapshot = Snapshot(version, content)
    if case.day_zero is None and (zero := first_day_zero([snapshot])) is not None:
        case.day_zero = zero
        _audit(
            session,
            case,
            actor,
            ctx,
            action="case.day_zero_set",
            version=version,
            new_value=zero.isoformat(),
        )

    tenant = session.get(TenantRow, case.tenant_id)
    regulators = tenant.regulators if tenant else []
    started = [_obligation(row) for row in list_clocks(session, case.id)]
    for obligation in ClockTracker(ctx.registry, regulators, started).observe(snapshot):
        session.add(
            CaseClockRow(
                tenant_id=case.tenant_id,
                case_id=case.id,
                case_version=obligation.case_version,
                regulator=obligation.regulator,
                rule_id=obligation.rule_id,
                rule_set_version=obligation.rule_set_version,
                clock_start=obligation.clock_start,
                due_date=obligation.due_date,
                created_at=ctx.now,
            )
        )
        _audit(
            session,
            case,
            actor,
            ctx,
            action="case.clock_started",
            version=version,
            new_value={
                "regulator": obligation.regulator,
                "rule_id": obligation.rule_id,
                "rule_set_version": obligation.rule_set_version,
                "clock_start": obligation.clock_start.isoformat(),
                "due_date": obligation.due_date.isoformat(),
            },
        )
        _enqueue(
            session,
            case,
            "case.clock_started",
            ctx,
            version=version,
            regulator=obligation.regulator,
            rule_id=obligation.rule_id,
            due_date=obligation.due_date.isoformat(),
        )
    session.flush()


def _product_codes(content: SafetyCase) -> list[str]:
    codes = {
        d.product_code.value
        for d in content.drugs
        if d.product_code is not None
        and d.role is not None
        and d.role.value in (DrugRole.SUSPECT, DrugRole.INTERACTING)
    }
    return sorted(codes)


def _obligation(row: CaseClockRow) -> Obligation:
    return Obligation(
        regulator=row.regulator,
        rule_id=row.rule_id,
        rule_set_version=row.rule_set_version,
        case_version=row.case_version,
        clock_start=row.clock_start,
        due_date=row.due_date,
    )


def _audit(
    session: Session,
    case: CaseRow,
    actor: Actor,
    ctx: CaseContext,
    *,
    action: str,
    version: int,
    field: str | None = None,
    old_value: Any = None,
    new_value: Any = None,
    reason: str | None = None,
) -> None:
    append_audit(
        session,
        case.tenant_id,
        AuditEvent(
            actor=actor,
            action=action,
            entity_type=ENTITY,
            entity_id=str(case.id),
            case_version=version,
            field=field,
            old_value=old_value,
            new_value=new_value,
            reason=reason,
        ),
        now=ctx.now,
    )


def _enqueue(
    session: Session, case: CaseRow, event_type: str, ctx: CaseContext, **payload: Any
) -> None:
    enqueue(
        session,
        case.tenant_id,
        OutboxEvent(
            topic=TOPIC,
            event_type=event_type,
            aggregate_id=str(case.id),
            payload={"case_id": str(case.id), "case_number": case.case_number, **payload},
        ),
        now=ctx.now,
    )
