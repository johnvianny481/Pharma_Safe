import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { IntakeForm, toPayload } from "./IntakeForm";

function renderForm() {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <IntakeForm accessToken="token-123" />
    </QueryClientProvider>,
  );
}

const ACK = {
  submission_id: "s1",
  duplicate: false,
  reports: [{ index: 0, case_id: "c1", case_number: "PS-000042" }],
};

afterEach(() => vi.unstubAllGlobals());

function sentKeys(fetchMock: ReturnType<typeof vi.fn>): string[] {
  return fetchMock.mock.calls.map(
    ([, init]) =>
      (init as RequestInit & { headers: Record<string, string> }).headers["Idempotency-Key"]!,
  );
}

test("submits the report with an idempotency key and shows the new case", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(ACK), { status: 201 }));
  vi.stubGlobal("fetch", fetchMock);
  renderForm();

  fireEvent.change(screen.getByLabelText("Initials"), { target: { value: "JD" } });
  fireEvent.change(screen.getByLabelText("Suspected medicine"), { target: { value: "Acmezol" } });
  fireEvent.change(screen.getByLabelText("Reaction"), { target: { value: "rash" } });
  fireEvent.change(screen.getByLabelText("Country (2 letters)"), { target: { value: "de" } });
  fireEvent.click(screen.getByRole("button", { name: "Submit report" }));

  expect(await screen.findByRole("status")).toHaveTextContent("Created case PS-000042.");
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(url).toBe("/api/intake/forms");
  const body = JSON.parse(init.body as string);
  expect(body.patient.initials).toBe("JD");
  expect(body.occurrence_country).toBe("DE");
  expect(body.reporter.given_name).toBeNull();
  expect(sentKeys(fetchMock)[0]).toMatch(/^[0-9a-f-]{36}$/);
});

test("a retry after a failure reuses the same idempotency key", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(new Response("", { status: 503 }))
    .mockResolvedValueOnce(new Response(JSON.stringify(ACK), { status: 201 }));
  vi.stubGlobal("fetch", fetchMock);
  renderForm();

  fireEvent.click(screen.getByRole("button", { name: "Submit report" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("will not be duplicated");
  fireEvent.click(screen.getByRole("button", { name: "Submit report" }));
  await screen.findByRole("status");

  const [first, second] = sentKeys(fetchMock);
  expect(second).toBe(first);
});

test("a new report gets a new idempotency key", async () => {
  const fetchMock = vi
    .fn()
    .mockImplementation(() => Promise.resolve(new Response(JSON.stringify(ACK), { status: 201 })));
  vi.stubGlobal("fetch", fetchMock);
  renderForm();

  fireEvent.click(screen.getByRole("button", { name: "Submit report" }));
  fireEvent.click(await screen.findByRole("button", { name: "Enter another report" }));
  fireEvent.click(screen.getByRole("button", { name: "Submit report" }));
  await screen.findByRole("status");

  const [first, second] = sentKeys(fetchMock);
  expect(second).not.toBe(first);
});

test("blank fields are sent as not given", () => {
  const payload = toPayload({
    given_name: "  ",
    family_name: "",
    email: "",
    telephone: "",
    qualification: "",
    initials: "",
    age: "",
    sex: "",
    product_name: "",
    reaction: "",
    onset_date: "",
    occurrence_country: "",
    description: "",
  });
  expect(payload.reporter.given_name).toBeNull();
  expect(payload.occurrence_country).toBeNull();
});
