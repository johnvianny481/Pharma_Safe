"""Token verification, roles and data scopes.

The tenant comes only from a verified, signed token (claim ``tenant_id``). Nothing in a
request body, query string or header can choose or change it.

Expected access-token claims (set by the Keycloak realm in ``deploy/keycloak``, or by the
production IdP broker):

- ``sub``, ``preferred_username``, ``name``
- ``tenant_id``: UUID of the caller's tenant (absent only for platform administrators)
- ``roles``: PharmaSafe roles, see :class:`Role`
- ``ps_products`` / ``ps_regions``: data scopes; ``"*"`` grants all
- ``auth_time``: when the user last actually authenticated (used for e-signatures)
- ``jti``: token id (used to make a signing re-authentication single-use)
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import jwt


class AuthenticationError(Exception):
    """The caller could not be authenticated (HTTP 401)."""


class AuthorizationError(Exception):
    """The caller is authenticated but not allowed to do this (HTTP 403)."""


class Role(StrEnum):
    INTAKE_PROCESSOR = "intake_processor"
    CASE_PROCESSOR = "case_processor"
    QUALITY_REVIEWER = "quality_reviewer"
    MEDICAL_REVIEWER = "medical_reviewer"
    TENANT_ADMIN = "tenant_admin"
    PLATFORM_ADMIN = "platform_admin"


ALL = "*"


@dataclass(frozen=True)
class DataScope:
    """Which products and regions a user may see inside their tenant.

    ``None`` means unrestricted (claim value ``"*"``). An empty set means nothing:
    a missing claim fails closed.
    """

    products: frozenset[str] | None
    regions: frozenset[str] | None

    @classmethod
    def from_claims(
        cls, products: Iterable[str] | None, regions: Iterable[str] | None
    ) -> "DataScope":
        return cls(products=_scope_values(products), regions=_scope_values(regions))

    def permits(self, product: str, region: str) -> bool:
        return _allows(self.products, product) and _allows(self.regions, region.lower())


def _scope_values(values: Iterable[str] | None) -> frozenset[str] | None:
    if values is None:
        return frozenset()
    items = frozenset(v.strip() for v in values if v.strip())
    if ALL in items:
        return None
    return items


def _allows(allowed: frozenset[str] | None, value: str) -> bool:
    return allowed is None or value in allowed


@dataclass(frozen=True)
class Principal:
    subject: str
    username: str
    display_name: str
    tenant_id: UUID | None
    roles: frozenset[Role]
    scope: DataScope
    auth_time: datetime | None
    token_id: str | None

    def has_any_role(self, *roles: Role) -> bool:
        return not self.roles.isdisjoint(roles)

    def require_tenant(self) -> UUID:
        if self.tenant_id is None:
            raise AuthorizationError("this action needs a tenant-scoped user")
        return self.tenant_id


KeyResolver = Callable[[str], Any]


def jwks_key_resolver(jwks_url: str) -> KeyResolver:
    """Resolve signing keys from the IdP's JWKS endpoint (cached by PyJWT)."""
    client = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=300)

    def resolve(token: str) -> Any:
        return client.get_signing_key_from_jwt(token).key

    return resolve


class TokenVerifier:
    def __init__(
        self,
        issuer: str,
        audience: str,
        key_resolver: KeyResolver,
        *,
        algorithms: tuple[str, ...] = ("RS256",),
        leeway_seconds: int = 30,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._key_resolver = key_resolver
        self._algorithms = list(algorithms)
        self._leeway = leeway_seconds

    def verify(self, token: str) -> Principal:
        try:
            key = self._key_resolver(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=self._algorithms,
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "iss", "sub", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationError(f"invalid token: {exc}") from exc
        return principal_from_claims(claims)


def principal_from_claims(claims: dict[str, Any]) -> Principal:
    known = {role.value for role in Role}
    roles = frozenset(Role(r) for r in _str_list(claims.get("roles")) or [] if r in known)
    tenant_id = _tenant(claims.get("tenant_id"))
    if tenant_id is None and Role.PLATFORM_ADMIN not in roles:
        raise AuthenticationError("token has no tenant_id")
    auth_time = claims.get("auth_time")
    return Principal(
        subject=str(claims["sub"]),
        username=str(claims.get("preferred_username") or claims["sub"]),
        display_name=str(claims.get("name") or claims.get("preferred_username") or claims["sub"]),
        tenant_id=tenant_id,
        roles=roles,
        scope=DataScope.from_claims(
            _str_list(claims.get("ps_products")), _str_list(claims.get("ps_regions"))
        ),
        auth_time=datetime.fromtimestamp(int(auth_time), tz=UTC) if auth_time is not None else None,
        token_id=str(claims["jti"]) if claims.get("jti") else None,
    )


def _tenant(value: Any) -> UUID | None:
    if value in (None, ""):
        return None
    try:
        return UUID(str(value))
    except ValueError as exc:
        raise AuthenticationError("tenant_id claim is not a UUID") from exc


def _str_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    raise AuthenticationError("expected a string or list claim")
