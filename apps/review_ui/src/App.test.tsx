import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { App } from "./App";

function renderApp() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

test("renders the heading and API status", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: "ok", service: "api" }))),
  );
  renderApp();
  expect(screen.getByRole("heading", { name: "PharmaSafe Review" })).toBeInTheDocument();
  expect(await screen.findByText("API: ok")).toBeInTheDocument();
});

test("shows unreachable when the API fails", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("", { status: 503 })));
  renderApp();
  expect(await screen.findByText("API: unreachable")).toBeInTheDocument();
});
