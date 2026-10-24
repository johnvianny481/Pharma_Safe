"""Electronic signatures: re-authentication, binding to one record version, immutability."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from pharmasafe_platform.audit import list_entries, verify_chain
from pharmasafe_platform.auth import AuthorizationError, Principal
from pharmasafe_platform.db import Database
from pharmasafe_platform.models import ESignatureRow
from pharmasafe_platform.signatures import (
    ReauthenticationRequired,
    ReauthenticationReused,
    SignableRecord,
    SignatureMeaning,
    SignatureService,
    content_hash,
)

PrincipalFactory = Callable[..., Principal]
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
SERVICE = SignatureService(max_auth_age=timedelta(minutes=5))

CASE_V1 = {"patient": {"age": 42}, "reactions": ["rash"], "version": 1}
RECORD_V1 = SignableRecord("case", "case-1", 1, content_hash(CASE_V1))


@pytest.fixture
def people(make_principal: PrincipalFactory, tenant_a: UUID) -> dict[str, Any]:
    def person(sub: str, roles: list[str], **kw: Any) -> Principal:
        return make_principal(sub=sub, tenant_id=str(tenant_a), roles=roles, **kw)

    signer = person(
        "proc-1", ["case_processor"], auth_time=int((NOW - timedelta(hours=2)).timestamp())
    )
    return {
        "signer": signer,
        # A fresh login with the IdP, moments before signing.
        "reauth": person("proc-1", ["case_processor"], auth_time=int((NOW - timedelta(seconds=20)).timestamp())),
        "medic": person("med-1", ["medical_reviewer"], auth_time=int(NOW.timestamp())),
        "other_user_reauth": person("proc-2", ["case_processor"], auth_time=int(NOW.timestamp())),
    }  # fmt: skip


def _sign(db: Database, tenant: UUID, people: dict[str, Any], **kw: Any) -> ESignatureRow:
    args: dict[str, Any] = {
        "signer": people["signer"],
        "reauth": people["reauth"],
        "record": RECORD_V1,
        "meaning": SignatureMeaning.REVIEWED,
        "reason": "data entry complete",
        "now": NOW,
    }
    args.update(kw)
    with db.tenant_session(tenant) as session:
        return SERVICE.sign(session, **args)


def test_signature_records_who_when_and_meaning(
    db: Database, tenant_a: UUID, people: dict[str, Any]
) -> None:
    sig = _sign(db, tenant_a, people)
    with db.tenant_session(tenant_a) as session:
        stored = session.get(ESignatureRow, sig.id)
    assert stored is not None
    assert stored.signer_id == "proc-1"
    assert stored.signer_name == "User One"
    assert stored.signed_at == NOW
    assert stored.meaning == "Reviewed"
    assert stored.entity_version == 1


def test_signature_verifies_for_the_signed_version(
    db: Database, tenant_a: UUID, people: dict[str, Any]
) -> None:
    sig = _sign(db, tenant_a, people)
    assert SERVICE.verify(sig, RECORD_V1).valid


@pytest.mark.parametrize(
    ("record", "problem"),
    [
        (replace(RECORD_V1, version=2), "different version"),
        (
            replace(RECORD_V1, content_hash=content_hash({**CASE_V1, "patient": {"age": 43}})),
            "content changed",
        ),
        (replace(RECORD_V1, entity_id="case-2"), "different record"),
    ],
)
def test_signature_does_not_transfer(
    db: Database, tenant_a: UUID, people: dict[str, Any], record: SignableRecord, problem: str
) -> None:
    sig = _sign(db, tenant_a, people)
    check = SERVICE.verify(sig, record)
    assert not check.valid
    assert problem in (check.problem or "")


def test_altered_signature_record_is_detected(
    db: Database, tenant_a: UUID, people: dict[str, Any]
) -> None:
    sig = _sign(db, tenant_a, people)
    sig.meaning = SignatureMeaning.MEDICALLY_APPROVED.value
    check = SERVICE.verify(sig, RECORD_V1)
    assert not check.valid
    assert check.problem == "signature record has been altered"


def test_signing_writes_an_audit_entry(
    db: Database, tenant_a: UUID, people: dict[str, Any]
) -> None:
    sig = _sign(db, tenant_a, people)
    with db.tenant_session(tenant_a) as session:
        (entry,) = list_entries(session, entity_type="case", entity_id="case-1")
        chain = verify_chain(session, tenant_a)
    assert entry.action == "signature.applied"
    assert entry.case_version == 1
    assert entry.new_value["signature_id"] == str(sig.id)
    assert entry.reason == "data entry complete"
    assert chain.ok


@pytest.mark.parametrize(
    ("reauth_key", "overrides"),
    [
        ("signer", {}),  # the session login from hours ago is not a re-authentication
        ("reauth", {"auth_time": NOW - timedelta(minutes=6)}),
        ("reauth", {"auth_time": NOW + timedelta(minutes=2)}),
        ("reauth", {"auth_time": None}),
        ("reauth", {"token_id": None}),
        ("other_user_reauth", {}),
    ],
)
def test_signing_requires_fresh_reauthentication_by_the_signer(
    db: Database,
    tenant_a: UUID,
    people: dict[str, Any],
    reauth_key: str,
    overrides: dict[str, Any],
) -> None:
    reauth = replace(people[reauth_key], **overrides)
    with pytest.raises(ReauthenticationRequired):
        _sign(db, tenant_a, people, reauth=reauth)


def test_reauthentication_from_another_tenant_is_rejected(
    db: Database, tenant_a: UUID, tenant_b: UUID, people: dict[str, Any]
) -> None:
    reauth = replace(people["reauth"], tenant_id=tenant_b)
    with pytest.raises(ReauthenticationRequired):
        _sign(db, tenant_a, people, reauth=reauth)


def test_reauthentication_is_single_use(
    db: Database, tenant_a: UUID, people: dict[str, Any]
) -> None:
    _sign(db, tenant_a, people)
    with pytest.raises(ReauthenticationReused):
        _sign(db, tenant_a, people, record=replace(RECORD_V1, entity_id="case-2"))


def test_role_must_match_meaning(db: Database, tenant_a: UUID, people: dict[str, Any]) -> None:
    with pytest.raises(AuthorizationError):
        _sign(db, tenant_a, people, meaning=SignatureMeaning.MEDICALLY_APPROVED)


def test_medical_reviewer_can_medically_approve(
    db: Database, tenant_a: UUID, people: dict[str, Any]
) -> None:
    medic = people["medic"]
    sig = _sign(
        db,
        tenant_a,
        people,
        signer=medic,
        reauth=medic,
        meaning=SignatureMeaning.MEDICALLY_APPROVED,
    )
    assert sig.meaning == "Medically approved"


@pytest.mark.parametrize(
    "statement", ["UPDATE e_signatures SET meaning = 'Approved'", "DELETE FROM e_signatures"]
)
def test_signatures_are_immutable(
    db: Database, tenant_a: UUID, people: dict[str, Any], statement: str
) -> None:
    _sign(db, tenant_a, people)
    with (
        pytest.raises(DBAPIError, match="permission denied"),
        db.tenant_session(tenant_a) as session,
    ):
        session.execute(text(statement))


def test_signatures_are_tenant_isolated(
    db: Database, tenant_a: UUID, tenant_b: UUID, people: dict[str, Any]
) -> None:
    _sign(db, tenant_a, people)
    with db.tenant_session(tenant_b) as session:
        assert session.scalars(select(ESignatureRow)).all() == []
