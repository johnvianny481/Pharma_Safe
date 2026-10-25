"""Duplicate merge: the reviewer's decision, applied in one transaction.

The reviewer compares a duplicate candidate with its master case and picks what to keep
from the duplicate. :func:`merge_duplicate` then, all or nothing:

1. copies the picked values into the master: into its draft, or into a new follow-up
   version if its latest version is approved (new information on an approved case);
2. marks each copied value ``source: import``, unconfirmed, with its original evidence
   (which points at the duplicate's source document), so a person confirms it on the
   master like any other value;
3. e-signs the decision (meaning ``Duplicate merged``) on the duplicate's current version;
4. closes the duplicate: ``merged_into`` is set for good (a trigger refuses any change),
   its open tasks are cancelled, and ``case.merged`` tells its workflow to finish;
5. records ``case.merged`` on the duplicate and ``case.merge_received`` on the master in
   the audit ledger.

Picks are field paths in the duplicate: single fields (``/patient/age``,
``/admin/occurrence_country``, ``/narrative/case_narrative``) or whole items
(``/drugs/d1``, ``/reactions/r2``, ``/reporters/p1``, ``/tests/t1``). Causality items are
not copied: they refer to drug and reaction ids of the duplicate.

A duplicate with an approved version cannot be merged: it may already have been
submitted, and nullifying a submitted case is a submission (Phase 7).
"""

from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from pharmasafe_api.cases import service as cases
from pharmasafe_api.cases.models import CaseRow, CaseVersionRow
from pharmasafe_api.review import service as review
from pharmasafe_domain.case import SafetyCase
from pharmasafe_domain.changes import AddItem, ChangeError, SetField, tracked_value_at
from pharmasafe_platform.audit import Actor, AuditEvent, append_audit
from pharmasafe_platform.auth import Principal
from pharmasafe_platform.outbox import OutboxEvent, enqueue
from pharmasafe_platform.signatures import SignableRecord, SignatureMeaning, SignatureService

FIELD_SECTIONS = ("admin", "patient", "narrative")
ITEM_SECTIONS = ("reporters", "drugs", "reactions", "tests")


def _as_import(node: Any) -> Any:
    """The same value as an unconfirmed import, keeping its evidence."""
    if isinstance(node, dict):
        if "source" in node and "value" in node:
            return {"value": node["value"], "source": "import", "evidence": node.get("evidence")}
        return {key: _as_import(child) for key, child in node.items()}
    if isinstance(node, list):
        return [_as_import(child) for child in node]
    return node


def _changes(
    duplicate: dict[str, Any], master: dict[str, Any], picks: list[str]
) -> list[SetField | AddItem]:
    changes: list[SetField | AddItem] = []
    taken = {section: {item["id"] for item in master[section]} for section in ITEM_SECTIONS}
    for path in dict.fromkeys(picks):
        segments = path.strip("/").split("/")
        if len(segments) == 2 and segments[0] in FIELD_SECTIONS:
            try:
                value = tracked_value_at(SafetyCase.from_json(duplicate), path)
            except ChangeError as exc:
                raise cases.InvalidChange(str(exc)) from exc
            if value is None:
                raise cases.InvalidChange(f"{path} is empty on the duplicate")
            changes.append(SetField(path=path, value=_as_import(value)))
        elif len(segments) == 2 and segments[0] in ITEM_SECTIONS:
            section, item_id = segments
            item = next((i for i in duplicate[section] if i["id"] == item_id), None)
            if item is None:
                raise cases.InvalidChange(f"{path} is not on the duplicate")
            new_id, n = item_id, 1
            while new_id in taken[section]:
                new_id, n = f"m{n}-{item_id}"[:64], n + 1
            taken[section].add(new_id)
            changes.append(AddItem(path=f"/{section}", item={**_as_import(item), "id": new_id}))
        else:
            raise cases.InvalidChange(f"{path} cannot be merged")
    return changes


def merge_duplicate(
    session: Session,
    duplicate: CaseRow,
    master: CaseRow,
    *,
    picks: list[str],
    signer: Principal,
    reauth: Principal,
    signatures: SignatureService,
    ctx: cases.CaseContext,
    reason: str | None = None,
) -> CaseVersionRow:
    """Merge ``duplicate`` into ``master``. Returns the master version that received it."""
    if duplicate.id == master.id:
        raise cases.CaseConflict("a case cannot be merged into itself")
    for case in (duplicate, master):
        if case.merged_into is not None:
            raise cases.CaseConflict(f"{case.case_number} has already been merged")
    dup_versions = cases.list_versions(session, duplicate.id)
    if any(v.status == cases.APPROVED for v in dup_versions):
        raise cases.CaseConflict("an approved case cannot be merged; nullify it by submission")
    dup_version = cases.get_version(
        session, duplicate.id, duplicate.latest_version, for_update=True
    )
    dup_content = cases.case_of(dup_version)
    actor = Actor.from_principal(signer)
    why = reason or f"merged from duplicate {duplicate.case_number}"

    target = cases.get_version(session, master.id, master.latest_version, for_update=True)
    if target.status == cases.APPROVED:
        receipt = dup_content.admin.latest_receipt_date or dup_content.admin.initial_receipt_date
        received: date = receipt.value if receipt is not None else ctx.today
        target = cases.start_follow_up(
            session, master, receipt_date=received, principal=signer, ctx=ctx, reason=why
        )
    changes = _changes(dup_content.to_json(), cases.case_of(target).to_json(), picks)
    if changes:
        target = cases.apply_system_changes(
            session,
            master,
            target.version,
            expected_revision=target.revision,
            changes=changes,
            actor=actor,
            ctx=ctx,
            reason=why,
        )

    signature = signatures.sign(
        session,
        signer=signer,
        reauth=reauth,
        record=SignableRecord(
            cases.ENTITY, str(duplicate.id), dup_version.version, dup_version.content_hash
        ),
        meaning=SignatureMeaning.DUPLICATE_MERGED,
        reason=why,
        now=ctx.now,
    )
    duplicate.merged_into = master.id
    duplicate.merged_at = ctx.now
    duplicate.merged_by = actor.id
    session.flush()
    review.cancel_open_tasks(
        session, duplicate, ctx.now, reason=f"merged into {master.case_number}"
    )

    for case, action, value, version in (
        (duplicate, "case.merged", {"merged_into": str(master.id),
                                    "master_case_number": master.case_number,
                                    "picks": picks, "signature_id": str(signature.id)},
         dup_version.version),
        (master, "case.merge_received", {"duplicate": str(duplicate.id),
                                         "duplicate_case_number": duplicate.case_number,
                                         "picks": picks}, target.version),
    ):  # fmt: skip
        append_audit(
            session,
            case.tenant_id,
            AuditEvent(
                actor=actor,
                action=action,
                entity_type=cases.ENTITY,
                entity_id=str(case.id),
                case_version=version,
                new_value=value,
                reason=why,
            ),
            now=ctx.now,
        )
    enqueue(
        session,
        duplicate.tenant_id,
        OutboxEvent(
            topic=cases.TOPIC,
            event_type="case.merged",
            aggregate_id=str(duplicate.id),
            payload={
                "case_id": str(duplicate.id),
                "case_number": duplicate.case_number,
                "version": dup_version.version,
                "merged_into": str(master.id),
            },
        ),
        now=ctx.now,
    )
    return target
