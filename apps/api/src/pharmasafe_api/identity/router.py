from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

from pharmasafe_api.deps import CurrentPrincipal

router = APIRouter(tags=["identity"])


class Me(BaseModel):
    subject: str
    username: str
    display_name: str
    tenant_id: UUID | None
    roles: list[str]
    products: list[str] | None
    regions: list[str] | None


@router.get("/me", response_model=Me)
def me(principal: CurrentPrincipal) -> Me:
    """The caller's identity as the API sees it. ``null`` scopes mean unrestricted."""
    scope = principal.scope
    return Me(
        subject=principal.subject,
        username=principal.username,
        display_name=principal.display_name,
        tenant_id=principal.tenant_id,
        roles=sorted(principal.roles),
        products=None if scope.products is None else sorted(scope.products),
        regions=None if scope.regions is None else sorted(scope.regions),
    )
