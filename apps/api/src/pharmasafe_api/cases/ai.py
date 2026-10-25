"""The cases module's interface for AI results: runs, redactions and case vectors.

The AI worker calls these functions; it never touches the tables directly.

- **Runs** (``case_ai_runs``): one row per case version and kind (``pipeline`` or
  ``narrative``), recording the bundle version, the effective automation level, flags and
  the full result. Writing twice is a no-op, so a retried activity is safe. Each run is
  also an audit entry.
- **Redactions** (``case_redactions``): the token and segment maps, encrypted with the tenant's
  key and bound to its case version and document. Only this module opens it.
- **Vectors** (``case_vectors``): blocking keys and the similarity vector of a case's
  latest version, and the blocked similarity query for duplicate detection.
"""

import json
from dataclasses import asdict
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Float, cast, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from pharmasafe_api.cases.models import (
    CaseAiRunRow,
    CaseRedactionRow,
    CaseRow,
    CaseVectorRow,
    Vector,
)
from pharmasafe_domain.ai.automation import Level
from pharmasafe_domain.ai.duplicates import Keys
from pharmasafe_domain.case import SafetyCase
from pharmasafe_platform.audit import Actor, AuditEvent, append_audit
from pharmasafe_platform.encryption import Sealed, TenantCipher
from pharmasafe_platform.models import TenantRow

AI_ACTOR = Actor(id="system:ai-pipeline", name="AI pipeline")
PIPELINE = "pipeline"
NARRATIVE = "narrative"


def automation_level_of(session: Session, tenant_id: UUID) -> Level:
    return Level(session.get_one(TenantRow, tenant_id).ai_automation_level)


def personal_values(case: SafetyCase) -> list[str]:
    """Identifiers already recorded on the case, to redact wherever they appear."""
    values = []
    for reporter in case.reporters:
        for field in (
            reporter.given_name,
            reporter.family_name,
            reporter.email,
            reporter.telephone,
            reporter.address,
        ):
            if field is not None:
                values.append(str(field.value))
    if case.patient.record_number is not None:
        values.append(case.patient.record_number.value)
    return values


# ---------------------------------------------------------------- redactions


def _associated(case_id: UUID, version: int, document_id: str) -> str:
    return f"case_redaction:{case_id}:{version}:{document_id}"


def store_redaction(
    session: Session,
    case: CaseRow,
    version: int,
    document_id: str,
    mapping: dict[str, Any],
    cipher: TenantCipher,
    now: datetime,
) -> None:
    sealed = cipher.seal(
        case.tenant_id,
        json.dumps(mapping, sort_keys=True).encode(),
        _associated(case.id, version, document_id),
    )
    session.execute(
        insert(CaseRedactionRow)
        .values(
            id=uuid4(),
            tenant_id=case.tenant_id,
            case_id=case.id,
            case_version=version,
            document_id=document_id,
            key_id=sealed.key_id,
            ciphertext=sealed.ciphertext,
            created_at=now,
        )
        .on_conflict_do_nothing()
    )


def reveal_redaction(
    session: Session, case: CaseRow, version: int, document_id: str, cipher: TenantCipher
) -> dict[str, Any] | None:
    row = session.scalars(
        select(CaseRedactionRow).where(
            CaseRedactionRow.case_id == case.id,
            CaseRedactionRow.case_version == version,
            CaseRedactionRow.document_id == document_id,
        )
    ).first()
    if row is None:
        return None
    plain = cipher.open(
        case.tenant_id,
        Sealed(row.key_id, row.ciphertext),
        _associated(case.id, version, document_id),
    )
    mapping: dict[str, Any] = json.loads(plain)
    return mapping


# ---------------------------------------------------------------- runs


def record_run(
    session: Session,
    case: CaseRow,
    version: int,
    *,
    kind: str,
    bundle_version: str,
    status: str,
    level: Level,
    flags: list[str],
    result: dict[str, Any],
    now: datetime,
) -> bool:
    """Record a pipeline or narrative run once. Returns False if it was already recorded."""
    inserted = session.execute(
        insert(CaseAiRunRow)
        .values(
            id=uuid4(),
            tenant_id=case.tenant_id,
            case_id=case.id,
            case_version=version,
            kind=kind,
            bundle_version=bundle_version,
            status=status,
            automation_level=level.value,
            flags=flags,
            result=result,
            created_at=now,
        )
        .on_conflict_do_nothing()
        .returning(CaseAiRunRow.id)
    ).scalar_one_or_none()
    if inserted is None:
        return False
    append_audit(
        session,
        case.tenant_id,
        AuditEvent(
            actor=AI_ACTOR,
            action=f"case.ai_{kind}_recorded",
            entity_type="case",
            entity_id=str(case.id),
            case_version=version,
            new_value={
                "bundle_version": bundle_version,
                "status": status,
                "automation_level": level.value,
                "flags": flags,
            },
        ),
        now=now,
    )
    return True


def run_of(session: Session, case_id: UUID, version: int, kind: str) -> CaseAiRunRow | None:
    return session.scalars(
        select(CaseAiRunRow).where(
            CaseAiRunRow.case_id == case_id,
            CaseAiRunRow.case_version == version,
            CaseAiRunRow.kind == kind,
        )
    ).first()


# ---------------------------------------------------------------- vectors


def keys_to_json(keys: Keys) -> dict[str, Any]:
    data = asdict(keys)
    return {k: sorted(v) if isinstance(v, frozenset) else v for k, v in data.items()}


def keys_from_json(data: dict[str, Any]) -> Keys:
    return Keys(
        drugs=frozenset(data.get("drugs", ())),
        reactions=frozenset(data.get("reactions", ())),
        onsets=frozenset(data.get("onsets", ())),
        initials=data.get("initials"),
        sex=data.get("sex"),
        age=data.get("age"),
        country=data.get("country"),
    )


def upsert_vector(
    session: Session,
    case: CaseRow,
    version: int,
    keys: Keys,
    embedding: list[float],
    now: datetime,
) -> None:
    values = {
        "tenant_id": case.tenant_id,
        "case_version": version,
        "drugs": sorted(keys.drugs),
        "reactions": sorted(keys.reactions),
        "keys": keys_to_json(keys),
        "embedding": embedding,
        "updated_at": now,
    }
    statement = insert(CaseVectorRow).values(case_id=case.id, **values)
    session.execute(
        statement.on_conflict_do_update(index_elements=[CaseVectorRow.case_id], set_=values)
    )


def similar_cases(
    session: Session, case: CaseRow, keys: Keys, embedding: list[float], *, limit: int
) -> list[tuple[str, str, Keys, float]]:
    """Cases sharing a suspect drug or reaction, nearest by vector first.

    Returns (case id, case number, keys, cosine similarity).
    """
    if not keys.drugs and not keys.reactions:
        return []
    vector = cast(embedding, Vector())
    distance = CaseVectorRow.embedding.op("<=>", return_type=Float)(vector)
    rows = session.execute(
        select(CaseVectorRow.case_id, CaseRow.case_number, CaseVectorRow.keys, distance)
        .join(CaseRow, CaseRow.id == CaseVectorRow.case_id)
        .where(
            CaseVectorRow.case_id != case.id,
            func.vector_dims(CaseVectorRow.embedding) == len(embedding),
            CaseVectorRow.drugs.overlap(sorted(keys.drugs))
            | CaseVectorRow.reactions.overlap(sorted(keys.reactions)),
        )
        .order_by(distance)
        .limit(limit)
    ).all()
    return [
        (str(case_id), number, keys_from_json(data), 1.0 - float(dist))
        for case_id, number, data, dist in rows
    ]
