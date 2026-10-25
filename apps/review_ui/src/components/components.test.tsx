import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import type { CaseContent, Tracked } from "../api";
import { AuthContext, type Auth } from "../auth";
import { DeadlineRisk } from "../pages/QueuePage";
import { comparisonRows } from "./AiPanels";
import { CaseFormContext, FieldRow, type FormContext } from "./CaseForm";
import { ESignDialog } from "./ESignDialog";
import { segments, SourcePanel } from "./SourcePanel";

const AI: Tracked = {
  value: "JD",
  source: "ai",
  evidence: { document_id: "d1", start: 8, end: 10, text: "JD" },
  confidence: 0.55,
  confirmed_by: null,
  confirmed_at: null,
};

function auth(over: Partial<Auth> = {}): Auth {
  return {
    user: { access_token: "t", profile: { name: "Cara" } } as unknown as Auth["user"],
    loading: false, error: null, signIn: vi.fn(), signOut: vi.fn(),
    reauthenticate: vi.fn(() => Promise.resolve("fresh")), ...over,
  }; // prettier-ignore
}

test("evidence segments split text at highlight boundaries", () => {
  const out = segments(0, 20, [
    { start: 2, end: 6, path: "/a" },
    { start: 4, end: 9, path: "/b" },
  ]);
  expect(out.map((s) => [s.start, s.end, s.paths])).toEqual([
    [0, 2, []], [2, 4, ["/a"]], [4, 6, ["/a", "/b"]], [6, 9, ["/b"]], [9, 20, []],
  ]); // prettier-ignore
});

test("the source panel marks evidence and the active field", () => {
  render(
    <SourcePanel
      source={{ document_id: "d1", filename: null, content_type: "text/plain",
                text: "Patient JD had a rash.", report_start: 0, report_end: 22, language: "en" }}
      highlights={[{ start: 8, end: 10, path: "/patient/initials" }, { start: 17, end: 21, path: "/r" }]}
      active="/patient/initials"
    />, // prettier-ignore
  );
  const marks = document.querySelectorAll("mark");
  expect([...marks].map((m) => m.textContent)).toEqual(["JD", "rash"]);
  expect(marks[0]).toHaveClass("evidence-active");
  expect(marks[1]).not.toHaveClass("evidence-active");
});

test("a field shows provenance, low confidence, and confirms on request", async () => {
  const save = vi.fn(() => Promise.resolve());
  const showEvidence = vi.fn();
  const ctx: FormContext = {
    content: {} as CaseContent, editable: true, save, showEvidence, activeEvidence: null,
  }; // prettier-ignore
  render(
    <CaseFormContext.Provider value={ctx}>
      <FieldRow spec={{ name: "initials", label: "Initials", kind: { type: "text" } }}
                path="/patient/initials" field={AI} />
    </CaseFormContext.Provider>, // prettier-ignore
  );
  expect(screen.getByText("AI")).toBeInTheDocument();
  expect(screen.getByLabelText("Confidence 55 percent, low")).toBeInTheDocument();
  expect(screen.getByText("Unconfirmed")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Show evidence for Initials" }));
  expect(showEvidence).toHaveBeenCalledWith("/patient/initials");
  fireEvent.click(screen.getByRole("button", { name: "Confirm Initials" }));
  await waitFor(() => expect(save).toHaveBeenCalledWith([{ op: "confirm", path: "/patient/initials" }]));
});

test("an edited value is saved as a person's value", async () => {
  const save = vi.fn(() => Promise.resolve());
  const ctx: FormContext = {
    content: {} as CaseContent, editable: true, save, showEvidence: vi.fn(), activeEvidence: null,
  }; // prettier-ignore
  render(
    <CaseFormContext.Provider value={ctx}>
      <FieldRow spec={{ name: "age", label: "Age", kind: { type: "age" } }} path="/patient/age" field={null} />
    </CaseFormContext.Provider>,
  );
  fireEvent.change(screen.getByLabelText("Age"), { target: { value: "64" } });
  fireEvent.click(screen.getByRole("button", { name: "Save age" }));
  await waitFor(() =>
    expect(save).toHaveBeenCalledWith([{ op: "set", path: "/patient/age", value: { value: "64", unit: "a" } }]),
  );
});

test("signing re-authenticates first and sends the fresh token with the reason", async () => {
  const onSign = vi.fn(() => Promise.resolve());
  const reauthenticate = vi.fn(() => Promise.resolve("fresh-token"));
  const onOpenChange = vi.fn();
  render(
    <AuthContext.Provider value={auth({ reauthenticate })}>
      <ESignDialog isOpen onOpenChange={onOpenChange} meaning="Approved" subject="PS-1, version 1"
                   onSign={onSign} />
    </AuthContext.Provider>, // prettier-ignore
  );
  expect(screen.getByRole("dialog")).toHaveTextContent("You are signing PS-1, version 1 with the meaning “Approved”");
  fireEvent.change(screen.getByLabelText("Reason (optional)"), { target: { value: "verified" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign: Approved" }));
  await waitFor(() => expect(onSign).toHaveBeenCalledWith("fresh-token", "verified"));
  expect(reauthenticate).toHaveBeenCalledOnce();
  expect(onOpenChange).toHaveBeenCalledWith(false);
});

test("a cancelled re-authentication signs nothing and says why", async () => {
  const onSign = vi.fn();
  render(
    <AuthContext.Provider value={auth({ reauthenticate: () => Promise.reject(new Error("Popup closed by user")) })}>
      <ESignDialog isOpen onOpenChange={vi.fn()} meaning="Reviewed" subject="PS-1" onSign={onSign} />
    </AuthContext.Provider>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Sign: Reviewed" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Popup closed by user");
  expect(onSign).not.toHaveBeenCalled();
});

test("deadline risk reads days left to the due date", () => {
  const today = new Date(2026, 9, 25);
  render(<><DeadlineRisk due="2026-10-24" today={today} /><DeadlineRisk due="2026-11-20" today={today} /><DeadlineRisk due={null} /></>);
  expect(screen.getByText("Overdue by 1 d (due 2026-10-24)")).toBeInTheDocument();
  expect(screen.getByText("Due in 26 d (2026-11-20)")).toBeInTheDocument();
  expect(screen.getByText("No deadline")).toBeInTheDocument();
});

test("merge comparison lists this case's values next to the master's", () => {
  const field = (value: unknown): Tracked => ({ ...AI, value, source: "human", confidence: null });
  const mine = {
    admin: {}, patient: { weight_kg: field("81") }, reporters: [], drugs: [],
    reactions: [{ id: "r1", reported_term: field("headache") }], tests: [], narrative: {},
  } as unknown as CaseContent; // prettier-ignore
  const theirs = {
    admin: {}, patient: { weight_kg: field("80") }, reporters: [], drugs: [],
    reactions: [{ id: "r1", reported_term: field("rash") }], tests: [], narrative: {},
  } as unknown as CaseContent; // prettier-ignore
  expect(comparisonRows(mine, theirs)).toEqual([
    { path: "/patient/weight_kg", label: "Weight (kg)", mine: "81", theirs: "80" },
    { path: "/reactions/r1", label: "Reaction: headache", mine: "whole item", theirs: "rash" },
  ]);
});
