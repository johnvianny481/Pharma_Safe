"""Electronic signatures (21 CFR 11.50, 11.70, 11.200; EU Annex 11 §14).

A signature records who signed, when, and what the signature means, and is bound to one
exact version of one record through that version's content hash. If the record changes,
or a later version is presented, the signature no longer verifies.

Signing needs a fresh re-authentication. The client makes the user log in again with the
IdP (OIDC ``prompt=login`` / ``max_age=0``) and sends that new token as proof. The proof
must belong to the same user and tenant, be no older than the configured maximum age, and
can be used for one signature only (its ``jti`` is stored with a unique constraint).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pharmasafe_platform.audit import Actor, AuditEvent, append_audit
from pharmasafe_platform.auth import AuthorizationError, Principal, Role
from pharmasafe_platform.hashing import sha256_hex
from pharmasafe_platform.models import ESignatureRow


class SignatureMeaning(StrEnum):
    REVIEWED = "Reviewed"
    QUALITY_REVIEWED = "Quality reviewed"
    MEDICALLY_APPROVED = "Medically approved"
    APPROVED = "Approved"


# Which roles may apply each meaning. Workflow rules (e.g. four-eyes) are added in Phase 4.
MEANING_ROLES: Mapping[SignatureMeaning, frozenset[Role]] = {
    SignatureMeaning.REVIEWED: frozenset({Role.CASE_PROCESSOR}),
    SignatureMeaning.QUALITY_REVIEWED: frozenset({Role.QUALITY_REVIEWER}),
    SignatureMeaning.MEDICALLY_APPROVED: frozenset({Role.MEDICAL_REVIEWER}),
    SignatureMeaning.APPROVED: frozenset({Role.QUALITY_REVIEWER, Role.MEDICAL_REVIEWER}),
}

# A re-authentication stamped this far in the future is treated as clock skew, not fraud.
CLOCK_SKEW = timedelta(seconds=30)


class SignatureError(Exception):
    pass


class ReauthenticationRequired(SignatureError):
    """The re-authentication proof is missing, stale, or belongs to someone else."""


class ReauthenticationReused(SignatureError):
    """The re-authentication proof was already used for another signature."""


@dataclass(frozen=True)
class SignableRecord:
    """One exact version of something that can be signed (e.g. a case version)."""

    entity_type: str
    entity_id: str
    version: int
    content_hash: str


def content_hash(content: Mapping[str, Any]) -> str:
    """Hash of a record version's content, used to bind signatures to it."""
    return sha256_hex(content)


@dataclass(frozen=True)
class SignatureCheck:
    valid: bool
    problem: str | None = None


class SignatureService:
    def __init__(self, max_auth_age: timedelta) -> None:
        self._max_auth_age = max_auth_age

    def sign(
        self,
        session: Session,
        *,
        signer: Principal,
        reauth: Principal,
        record: SignableRecord,
        meaning: SignatureMeaning,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> ESignatureRow:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        tenant_id = signer.require_tenant()
        auth_time, token_id = self._check_reauth(signer, reauth, tenant_id, now)
        if signer.roles.isdisjoint(MEANING_ROLES[meaning]):
            raise AuthorizationError(f"your roles cannot sign with meaning '{meaning}'")

        row = ESignatureRow(
            id=uuid4(),
            tenant_id=tenant_id,
            entity_type=record.entity_type,
            entity_id=record.entity_id,
            entity_version=record.version,
            content_hash=record.content_hash,
            meaning=meaning.value,
            reason=reason,
            signer_id=signer.subject,
            signer_name=signer.display_name,
            signed_at=now,
            auth_time=auth_time,
            reauth_token_id=token_id,
        )
        row.signature_hash = signature_hash(row)
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError as exc:
            raise ReauthenticationReused("this re-authentication was already used to sign") from exc

        append_audit(
            session,
            tenant_id,
            AuditEvent(
                actor=Actor.from_principal(signer),
                action="signature.applied",
                entity_type=record.entity_type,
                entity_id=record.entity_id,
                case_version=record.version,
                new_value={
                    "signature_id": str(row.id),
                    "meaning": row.meaning,
                    "content_hash": row.content_hash,
                },
                reason=reason,
            ),
            now=now,
        )
        return row

    def _check_reauth(
        self, signer: Principal, reauth: Principal, tenant_id: UUID, now: datetime
    ) -> tuple[datetime, str]:
        if reauth.subject != signer.subject or reauth.tenant_id != tenant_id:
            raise ReauthenticationRequired("re-authentication belongs to a different user")
        if reauth.auth_time is None or reauth.token_id is None:
            raise ReauthenticationRequired("re-authentication proof is incomplete")
        age = now - reauth.auth_time
        if age > self._max_auth_age or age < -CLOCK_SKEW:
            raise ReauthenticationRequired("re-authentication is too old; log in again to sign")
        return reauth.auth_time, reauth.token_id

    @staticmethod
    def verify(signature: ESignatureRow, record: SignableRecord) -> SignatureCheck:
        """Check that a stored signature is intact and applies to exactly this record version."""
        if signature_hash(signature) != signature.signature_hash:
            return SignatureCheck(False, "signature record has been altered")
        if (signature.entity_type, signature.entity_id) != (record.entity_type, record.entity_id):
            return SignatureCheck(False, "signature belongs to a different record")
        if signature.entity_version != record.version:
            return SignatureCheck(False, "signature belongs to a different version")
        if signature.content_hash != record.content_hash:
            return SignatureCheck(False, "record content changed after signing")
        return SignatureCheck(True)


def signature_hash(row: ESignatureRow) -> str:
    return sha256_hex(
        {
            "id": str(row.id),
            "tenant_id": str(row.tenant_id),
            "entity_type": row.entity_type,
            "entity_id": row.entity_id,
            "entity_version": row.entity_version,
            "content_hash": row.content_hash,
            "meaning": row.meaning,
            "reason": row.reason,
            "signer_id": row.signer_id,
            "signer_name": row.signer_name,
            "signed_at": row.signed_at.astimezone(UTC).isoformat(timespec="microseconds"),
            "auth_time": row.auth_time.astimezone(UTC).isoformat(timespec="microseconds"),
            "reauth_token_id": row.reauth_token_id,
        }
    )
