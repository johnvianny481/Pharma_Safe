import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import type { User } from "oidc-client-ts";
import { afterEach, expect, test, vi } from "vitest";

import { App } from "./App";
import { AuthContext, type Auth } from "./auth";

function renderApp(auth: Partial<Auth>) {
  const value: Auth = {
    user: null,
    loading: false,
    error: null,
    signIn: vi.fn(() => Promise.resolve()),
    signOut: vi.fn(() => Promise.resolve()),
    reauthenticate: vi.fn(() => Promise.resolve("fresh-token")),
    ...auth,
  };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <AuthContext.Provider value={value}>
      <QueryClientProvider client={client}>
        <App />
      </QueryClientProvider>
    </AuthContext.Provider>,
  );
  return value;
}

const signedIn = { access_token: "token-123" } as User;

const ME = {
  subject: "s1",
  username: "acme.processor",
  display_name: "Cara Processor",
  tenant_id: "11111111-1111-4111-8111-111111111111",
  roles: ["case_processor"],
  products: null,
  regions: ["eu"],
};

/** A fake API answering by path. */
function api(routes: Record<string, unknown>, status = 200) {
  return vi.fn((url: string) => {
    const path = url.replace(/^\/api/, "").split("?")[0] ?? "";
    if (status !== 200) return Promise.resolve(new Response("", { status }));
    return Promise.resolve(new Response(JSON.stringify(routes[path] ?? [])));
  });
}

afterEach(() => vi.unstubAllGlobals());

test("offers sign-in when there is no session", () => {
  const auth = renderApp({});
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  expect(auth.signIn).toHaveBeenCalledOnce();
});

test("shows the identity the API returns, sending the access token", async () => {
  const fetchMock = api({ "/me": ME, "/tasks": [] });
  vi.stubGlobal("fetch", fetchMock);
  renderApp({ user: signedIn });

  expect(await screen.findByText("Cara Processor (acme.processor)")).toBeInTheDocument();
  expect(screen.getByText("case_processor")).toBeInTheDocument();
  expect(screen.getByText("all")).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith(
    "/api/me",
    expect.objectContaining({ headers: { Authorization: "Bearer token-123" } }),
  );
  expect(await screen.findByText("Nothing to do right now.")).toBeInTheDocument();
  // Case processors may enter reports; only tenant admins see administration.
  expect(screen.getByRole("link", { name: "New report" })).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Administration" })).not.toBeInTheDocument();
});

test("lists open tasks, most urgent first, with deadline risk and AI flags", async () => {
  const due = new Date(Date.now() + 86_400_000).toISOString().slice(0, 10);
  const task = {
    id: "t1", case_id: "c1", case_number: "PS-1", case_version: 1, kind: "data_entry",
    status: "open", assignee: null, created_at: "", next_due: due, case_stage: "data_entry_review",
    flags: ["serious_by_signals"],
  }; // prettier-ignore
  vi.stubGlobal("fetch", api({ "/me": ME, "/tasks": [task] }));
  renderApp({ user: signedIn });

  expect(await screen.findByRole("link", { name: "PS-1" })).toHaveAttribute("href", "/cases/c1");
  expect(screen.getByText("Data entry review")).toBeInTheDocument();
  expect(screen.getByText(/Due in 1 d/)).toBeInTheDocument();
  expect(screen.getByText("Serious (AI or rules)")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Claim PS-1" })).toBeInTheDocument();
});

test("reports an API failure", async () => {
  vi.stubGlobal("fetch", api({}, 401));
  renderApp({ user: signedIn });
  const alerts = await screen.findAllByRole("alert");
  expect(alerts.map((a) => a.textContent).join(" ")).toContain("API returned 401");
});

test("reports a sign-in error", () => {
  renderApp({ error: "state mismatch" });
  expect(screen.getByRole("alert")).toHaveTextContent("Sign-in failed: state mismatch");
});
