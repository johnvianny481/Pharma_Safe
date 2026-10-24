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

afterEach(() => vi.unstubAllGlobals());

test("offers sign-in when there is no session", () => {
  const auth = renderApp({});
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  expect(auth.signIn).toHaveBeenCalledOnce();
});

test("shows the identity the API returns, sending the access token", async () => {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(
      JSON.stringify({
        subject: "s1",
        username: "acme.processor",
        display_name: "Cara Processor",
        tenant_id: "11111111-1111-4111-8111-111111111111",
        roles: ["case_processor"],
        products: null,
        regions: ["eu"],
      }),
    ),
  );
  vi.stubGlobal("fetch", fetchMock);
  renderApp({ user: signedIn });

  expect(await screen.findByText("Cara Processor (acme.processor)")).toBeInTheDocument();
  expect(screen.getByText("case_processor")).toBeInTheDocument();
  expect(screen.getByText("all")).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith("/api/me", {
    headers: { Authorization: "Bearer token-123" },
  });
});

test("reports an API failure", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("", { status: 401 })));
  renderApp({ user: signedIn });
  expect(await screen.findByRole("alert")).toHaveTextContent("API returned 401");
});

test("reports a sign-in error", () => {
  renderApp({ error: "state mismatch" });
  expect(screen.getByRole("alert")).toHaveTextContent("Sign-in failed: state mismatch");
});
