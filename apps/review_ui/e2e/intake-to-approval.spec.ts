/**
 * Intake to approval, through every hand-off:
 * intake form → AI pre-fill (MockLLM) → data entry review (confirm, assess, e-sign)
 * → approval by another person (e-sign) → approved, locked version.
 */
import { expect, test } from "@playwright/test";

import { approve, disableQcSampling, report } from "./flows";
import {
  assessSeriousness,
  confirmEverything,
  enterReport,
  openFromQueue,
  signIn,
  signWithReauth,
  unique,
  waitForAi,
} from "./support";

test.beforeAll(async ({ browser }) => disableQcSampling(browser));

test("a report goes from intake to an approved case", async ({ browser }) => {
  const intake = await signIn(browser, "acme.intake");
  const caseNumber = await enterReport(intake, report(unique("Testazol")));

  const processor = await signIn(browser, "acme.processor");
  await openFromQueue(processor, caseNumber);
  await waitForAi(processor);
  // The AI pre-filled the dose from the description, with evidence in the source.
  await expect(processor.getByLabel("Dose", { exact: true })).toHaveValue("20");
  await processor.getByRole("button", { name: "Show evidence for Dose" }).click();
  await expect(processor.locator("mark.evidence-active")).toContainText("20 mg");
  // Approval is blocked until every value is confirmed and seriousness is assessed.
  await expect(processor.getByText(/things to resolve before approval/)).toBeVisible();

  await assessSeriousness(processor);
  await confirmEverything(processor);
  await processor.getByRole("button", { name: "Sign and complete: Reviewed" }).click();
  await signWithReauth(processor, "acme.processor", "Sign: Reviewed");

  const quality = await signIn(browser, "acme.quality");
  await approve(quality, caseNumber);

  // Approved versions are read-only, and the approval is in the audit trail.
  await quality.reload();
  await expect(quality.getByRole("button", { name: /^Confirm/ })).toHaveCount(0);
  await quality.getByRole("tab", { name: "History and audit" }).click();
  await expect(quality.getByRole("row", { name: "Version 1" })).toContainText("approved");
  await expect(quality.getByText("case.version_approved (v1)")).toBeVisible();
});
