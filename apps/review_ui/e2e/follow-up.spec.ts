/**
 * Follow-up versioning: new information on an approved case starts version 2; version 1
 * stays exactly as approved, and both are in the history and audit trail.
 */
import { expect, test } from "@playwright/test";

import { approve, completeDataEntry, disableQcSampling, report } from "./flows";
import { enterReport, signIn, unique } from "./support";

test.beforeAll(async ({ browser }) => disableQcSampling(browser));

test("follow-up information creates a new version and keeps the approved one", async ({
  browser,
}) => {
  const intake = await signIn(browser, "acme.intake");
  const caseNumber = await enterReport(intake, report(unique("Followzol")));
  const processor = await signIn(browser, "acme.processor");
  await completeDataEntry(processor, caseNumber);
  const quality = await signIn(browser, "acme.quality");
  const caseUrl = await approve(quality, caseNumber);

  await processor.goto(caseUrl);
  await processor.getByRole("tab", { name: "History and audit" }).click();
  await processor.getByLabel("Follow-up receipt date").fill("2026-10-20");
  await processor.getByRole("button", { name: "Start follow-up version" }).click();
  await expect(processor.getByRole("row", { name: "Version 2" })).toContainText("draft");
  await expect(processor.getByRole("row", { name: "Version 1" })).toContainText("approved");

  // Version 2 is editable: record the follow-up information.
  await processor.getByRole("tab", { name: "Case" }).click();
  await expect(processor.getByRole("heading", { level: 1 })).toContainText("version 2");
  const narrative = processor.getByLabel("Case narrative (H.1)");
  await narrative.fill("Follow-up: the rash resolved after stopping treatment.");
  await processor.getByRole("button", { name: "Save narrative" }).click();
  await expect(processor.getByRole("button", { name: "Save narrative" })).toBeHidden();
  await expect(narrative).toHaveValue(/rash resolved/);

  // Version 1 is unchanged; the history shows both versions and the follow-up.
  await processor.getByRole("tab", { name: "History and audit" }).click();
  await processor.getByRole("button", { name: "View version 1" }).click();
  const v1 = processor.getByRole("region", { name: "Version 1 content" });
  await expect(v1).toContainText("approved");
  await expect(v1).not.toContainText("rash resolved");
  await expect(processor.getByText("case.version_created (v2)")).toBeVisible();
});
