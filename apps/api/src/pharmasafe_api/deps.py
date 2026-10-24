"""FastAPI dependencies for authentication, authorisation and tenant-scoped DB sessions."""

from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from pharmasafe_platform.auth import (
    AuthenticationError,
    AuthorizationError,
    Principal,
    Role,
    TokenVerifier,
)
from pharmasafe_platform.context import current_tenant_id
from pharmasafe_platform.db import Database
from pharmasafe_platform.telemetry import tag_current_span

bearer = HTTPBearer(auto_error=False, description="Access token from the PharmaSafe IdP")


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail=detail, headers={"WWW-Authenticate": "Bearer"}
    )


# async on purpose: context variables set here must stay visible to the endpoint. FastAPI
# runs sync dependencies in a worker thread, so changes they make would be lost.
async def get_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Principal:
    if credentials is None:
        raise _unauthorized("missing bearer token")
    verifier: TokenVerifier = request.app.state.verifier
    try:
        principal = verifier.verify(credentials.credentials)
    except AuthenticationError as exc:
        raise _unauthorized(str(exc)) from exc
    if principal.tenant_id is not None:
        tenant = str(principal.tenant_id)
        current_tenant_id.set(tenant)
        tag_current_span(tenant_id=tenant)
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def require_roles(*roles: Role) -> Callable[[Principal], Principal]:
    def check(principal: CurrentPrincipal) -> Principal:
        if not principal.has_any_role(*roles):
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="insufficient role")
        return principal

    return check


def get_tenant_session(request: Request, principal: CurrentPrincipal) -> Iterator[Session]:
    """A DB session bound to the caller's tenant, taken from the verified token only."""
    try:
        tenant_id = principal.require_tenant()
    except AuthorizationError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    database: Database = request.app.state.database
    with database.tenant_session(tenant_id) as session:
        yield session


TenantSession = Annotated[Session, Depends(get_tenant_session)]
