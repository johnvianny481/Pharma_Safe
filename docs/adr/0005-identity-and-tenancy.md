# ADR 0005: Identity, roles, data scopes and tenant isolation

- Status: Accepted
- Date: 2026-10-25

## Context

PharmaSafe is multi-tenant, and one customer's safety data must never reach another customer. The build prompt requires three things:

- the tenant comes only from the verified token;
- PostgreSQL row-level security (RLS) enforces isolation;
- tests prove that cross-tenant access fails.

Users have one of six roles and can be limited to certain products and regions.

## Decision

**Identity.** Users log in with OIDC authorization code + PKCE. Locally the IdP is Keycloak, using the `pharmasafe` realm in `deploy/keycloak/`. In production it is Entra ID or the customer's IdP, brokered so that tokens carry the same claims. The API is a resource server. It verifies RS256 signature, issuer, audience (`pharmasafe-api`) and expiry against the IdP's JWKS.

**Claims.** Tokens carry:

- `tenant_id` (a UUID)
- `roles` (client roles of `pharmasafe-api`)
- `ps_products` and `ps_regions` (data scopes, where `*` means all)
- `auth_time`
- `jti`

**Fail closed.** If a scope claim is missing, the user gets no scope, not all scopes. A token without `tenant_id` is rejected unless its only purpose is platform administration. A `platform_admin` gets no tenant data.

**User attributes are admin-edit only.** The realm's user profile uses `unmanagedAttributePolicy: ADMIN_EDIT`. Without this, users could edit their own `tenant_id` in the Keycloak account console and switch tenant.

**No password grant.** Only the authorization code flow is enabled. Keycloak does not set `auth_time` for the password grant, and that grant would bypass MFA and other IdP policies anyway.

**Tenant isolation in the database.**

- Every table with `tenant_id` has RLS enabled *and forced*. Its policy compares `tenant_id` with `app_current_tenant()`, which reads the transaction-local setting `app.tenant_id`.
- `Database.tenant_session()` sets that value at the start of every transaction from the verified principal. With no tenant set, the function returns NULL and no rows match.
- Requests connect as a login role that belongs to the NOLOGIN group `pharmasafe_app`. That role is not the owner and has no `BYPASSRLS`. Only migrations use the owner role.
- A test inspects the catalog and fails if any table with `tenant_id` lacks forced RLS and a policy. This protects future migrations.

**Tenant directory.** The `tenants` table holds no case or personal data. It has no RLS and is read-only for the app role, so system jobs (the audit verifier) can list tenants.

**Data scopes.** Scopes are parsed and enforced in code (`DataScope.permits`). They will also become query filters, and possibly RLS policies, once cases exist (Phase 2).

## Consequences

- A bug in application code cannot leak another tenant's rows, because the database refuses.
- Every request opens its transaction with one extra statement (`set_config`).
- Dedicated-database tenants (ADR to follow, Phase 10) keep the same code path. RLS stays on as defence in depth.
