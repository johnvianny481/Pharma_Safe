import { Link, RouterProvider } from "react-aria-components";
import { BrowserRouter, Route, Routes, useHref, useNavigate, type NavigateOptions } from "react-router";

import { useAuth } from "./auth";
import { Button, ErrorText } from "./components/ui";
import { IntakeForm } from "./IntakeForm";
import { AdminPage } from "./pages/AdminPage";
import { CasePage } from "./pages/CasePage";
import { QueuePage } from "./pages/QueuePage";
import { useMe } from "./queries";

declare module "react-aria-components" {
  interface RouterConfig {
    routerOptions: NavigateOptions;
  }
}

const INTAKE_ROLES = ["intake_processor", "case_processor"];

function scopeLabel(values: string[] | null): string {
  if (values === null) return "all";
  return values.length ? values.join(", ") : "none";
}

function Shell() {
  const auth = useAuth();
  const navigate = useNavigate();
  const me = useMe();
  const roles = me.data?.roles ?? [];

  return (
    <RouterProvider navigate={navigate} useHref={useHref}>
      <a href="#main" className="skip-link">Skip to main content</a>
      <header className="app-header">
        <p className="brand">PharmaSafe Review</p>
        <nav aria-label="Main">
          <Link href="/">Work queue</Link>
          {roles.some((r) => INTAKE_ROLES.includes(r)) && <Link href="/intake">New report</Link>}
          {roles.includes("tenant_admin") && <Link href="/admin">Administration</Link>}
        </nav>
        {me.isError && <ErrorText error={new Error(`Could not load your profile: ${me.error.message}`)} />}
        {me.data && (
          <dl className="profile" aria-label="Signed in as">
            <dt>User</dt>
            <dd>{me.data.display_name} ({me.data.username})</dd>
            <dt>Roles</dt>
            <dd>{me.data.roles.join(", ") || "none"}</dd>
            <dt>Products</dt>
            <dd>{scopeLabel(me.data.products)}</dd>
            <dt>Regions</dt>
            <dd>{scopeLabel(me.data.regions)}</dd>
          </dl>
        )}
        <Button variant="quiet" onPress={() => void auth.signOut()}>Sign out</Button>
      </header>
      <main id="main" tabIndex={-1}>
        <Routes>
          <Route path="/" element={<QueuePage />} />
          <Route path="/intake" element={<IntakePage />} />
          <Route path="/cases/:caseId" element={<CasePage />} />
          <Route path="/admin" element={<AdminPage />} />
          <Route path="*" element={<p>Page not found.</p>} />
        </Routes>
      </main>
    </RouterProvider>
  );
} // prettier-ignore

function IntakePage() {
  const { user } = useAuth();
  return user ? <IntakeForm accessToken={user.access_token} /> : null;
}

/** Sign-in, then the review workspace: queue, case workspace, intake and administration. */
export function App() {
  const auth = useAuth();
  if (auth.loading) return <p role="status">Checking your session…</p>;
  if (!auth.user) {
    return (
      <main id="main">
        <h1>PharmaSafe Review</h1>
        {auth.error && <p role="alert">Sign-in failed: {auth.error}</p>}
        <Button variant="primary" onPress={() => void auth.signIn()}>Sign in</Button>
      </main>
    );
  }
  return (
    <BrowserRouter>
      <Shell />
    </BrowserRouter>
  );
} // prettier-ignore
