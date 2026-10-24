from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from pharmasafe_platform.auth import (
    AuthenticationError,
    AuthorizationError,
    DataScope,
    Role,
    TokenVerifier,
)

TokenFactory = Callable[..., str]


def test_valid_token_yields_principal(verifier: TokenVerifier, issue_token: TokenFactory) -> None:
    tenant = UUID("11111111-1111-4111-8111-111111111111")
    principal = verifier.verify(
        issue_token(tenant_id=str(tenant), roles=["case_processor", "quality_reviewer"])
    )
    assert principal.subject == "user-1"
    assert principal.username == "user.one"
    assert principal.tenant_id == tenant
    assert principal.roles == {Role.CASE_PROCESSOR, Role.QUALITY_REVIEWER}
    assert principal.auth_time is not None
    assert principal.token_id is not None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"exp": int((datetime.now(UTC) - timedelta(minutes=5)).timestamp())}, "expired"),
        ({"aud": "some-other-api"}, "(?i)audience"),
        ({"iss": "https://evil.example/realms/pharmasafe"}, "issuer"),
        ({"sub": None}, "sub"),
        ({"tenant_id": None}, "tenant_id"),
        ({"tenant_id": "not-a-uuid"}, "UUID"),
    ],
)
def test_rejects_invalid_tokens(
    verifier: TokenVerifier, issue_token: TokenFactory, overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(AuthenticationError, match=message):
        verifier.verify(issue_token(**overrides))


def test_rejects_token_signed_by_another_key(verifier: TokenVerifier) -> None:
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(
        {"iss": "x", "aud": "pharmasafe-api", "sub": "a", "iat": 0, "exp": 2**31},
        other_key,
        "RS256",
    )
    with pytest.raises(AuthenticationError):
        verifier.verify(token)


def test_rejects_unsigned_token(verifier: TokenVerifier, issue_token: TokenFactory) -> None:
    claims = jwt.decode(issue_token(), options={"verify_signature": False})
    unsigned = jwt.encode(claims, None, algorithm="none")
    with pytest.raises(AuthenticationError):
        verifier.verify(unsigned)


def test_platform_admin_needs_no_tenant(verifier: TokenVerifier, issue_token: TokenFactory) -> None:
    principal = verifier.verify(issue_token(tenant_id=None, roles=["platform_admin"]))
    assert principal.tenant_id is None
    with pytest.raises(AuthorizationError):
        principal.require_tenant()


def test_unknown_roles_are_ignored(verifier: TokenVerifier, issue_token: TokenFactory) -> None:
    principal = verifier.verify(
        issue_token(roles=["case_processor", "offline_access", "superuser"])
    )
    assert principal.roles == {Role.CASE_PROCESSOR}


def test_single_string_role_claim_is_accepted(
    verifier: TokenVerifier, issue_token: TokenFactory
) -> None:
    assert verifier.verify(issue_token(roles="tenant_admin")).roles == {Role.TENANT_ADMIN}


class TestDataScope:
    def test_wildcard_allows_everything(self) -> None:
        scope = DataScope.from_claims(["*"], ["*"])
        assert scope.permits("ANY-PRODUCT", "in")

    def test_specific_scope(self) -> None:
        scope = DataScope.from_claims(["ACM-001"], ["eu"])
        assert scope.permits("ACM-001", "eu")
        assert scope.permits("ACM-001", "EU")
        assert not scope.permits("ACM-002", "eu")
        assert not scope.permits("ACM-001", "us")

    def test_missing_claims_fail_closed(self) -> None:
        scope = DataScope.from_claims(None, None)
        assert not scope.permits("ACM-001", "eu")

    def test_blank_values_are_dropped(self) -> None:
        assert DataScope.from_claims([" ", "ACM-001"], []).products == {"ACM-001"}

    def test_scope_claims_reach_principal(
        self, verifier: TokenVerifier, issue_token: TokenFactory
    ) -> None:
        principal = verifier.verify(issue_token(ps_products=["ACM-001"], ps_regions=None))
        assert principal.scope.products == {"ACM-001"}
        assert principal.scope.regions == frozenset()
