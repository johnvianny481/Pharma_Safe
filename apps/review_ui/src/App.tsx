import { useQuery } from "@tanstack/react-query";

import { apiGet, type Me } from "./api";
import { useAuth } from "./auth";
import { IntakeForm } from "./IntakeForm";

const INTAKE_ROLES = ["intake_processor", "case_processor"];

function scopeLabel(values: string[] | null): string {
  if (values === null) return "all";
  return values.length ? values.join(", ") : "none";
}

function Profile({ accessToken }: { accessToken: string }) {
  const me = useQuery({
    queryKey: ["me", accessToken],
    queryFn: () => apiGet<Me>("/me", accessToken),
    retry: false,
  });

  if (me.isPending) return <p role="status">Loading your profile…</p>;
  if (me.isError) return <p role="alert">Could not load your profile: {me.error.message}</p>;
  const canEnterReports = me.data.roles.some((role) => INTAKE_ROLES.includes(role));
  return (
    <>
      <dl>
        <dt>User</dt>
        <dd>
          {me.data.display_name} ({me.data.username})
        </dd>
        <dt>Tenant</dt>
        <dd>{me.data.tenant_id ?? "none (platform)"}</dd>
        <dt>Roles</dt>
        <dd>{me.data.roles.join(", ") || "none"}</dd>
        <dt>Products</dt>
        <dd>{scopeLabel(me.data.products)}</dd>
        <dt>Regions</dt>
        <dd>{scopeLabel(me.data.regions)}</dd>
      </dl>
      {canEnterReports && <IntakeForm accessToken={accessToken} />}
    </>
  );
}

/** Sign-in, identity and report intake. Queues and the case workspace arrive in Phase 6. */
export function App() {
  const auth = useAuth();

  return (
    <main>
      <h1>PharmaSafe Review</h1>
      {auth.error && <p role="alert">Sign-in failed: {auth.error}</p>}
      {auth.loading ? (
        <p role="status">Checking your session…</p>
      ) : auth.user ? (
        <>
          <Profile accessToken={auth.user.access_token} />
          <button type="button" onClick={() => void auth.signOut()}>
            Sign out
          </button>
        </>
      ) : (
        <button type="button" onClick={() => void auth.signIn()}>
          Sign in
        </button>
      )}
    </main>
  );
}
