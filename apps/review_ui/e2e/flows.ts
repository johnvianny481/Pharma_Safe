/** Multi-person flows reused by several end-to-end tests. */
import { expect, type Browser, type Page } from "@playwright/test";

import {
  assessSeriousness,
  confirmEverything,
  openFromQueue,
  type Report,
  signIn,
  signWithReauth,
  waitForAi,
} from "./support";

/** Admin setting: no QC sampling, so non-serious cases go from data entry to approval. */
export async function disableQcSampling(browser: Browser): Promise<void> {
  const admin = await signIn(browser, "acme.admin");
  await admin.getByRole("link", { name: "Administration" }).click();
  await admin.getByLabel(/QC sampling rate/).fill("0");
  await admin.getByLabel("Reason for the change").fill("end-to-end tests");
  await admin.getByRole("button", { name: "Save settings" }).click();
  await expect(admin.getByRole("status").filter({ hasText: "Settings saved." })).toBeVisible();
  await admin.context().close();
}

export function report(product: string, reaction = "rash", initials = "JD"): Report {
  return {
    initials,
    product,
    reaction,
    description:
      `Patient ${initials}, a 64-year-old woman, developed a mild ${reaction} on 2026-10-01` +
      ` after starting ${product} 20 mg.`,
  };
}

/** Data entry review of an open case: assess, confirm everything, sign "Reviewed". */
export async function completeDataEntry(processor: Page, caseNumber: string): Promise<void> {
  await openFromQueue(processor, caseNumber);
  await waitForAi(processor);
  await assessSeriousness(processor);
  await confirmEverything(processor);
  await processor.getByRole("button", { name: "Sign and complete: Reviewed" }).click();
  await signWithReauth(processor, "acme.processor", "Sign: Reviewed");
}

/** Approve the case as the quality reviewer; returns the case page URL. */
export async function approve(quality: Page, caseNumber: string): Promise<string> {
  await openFromQueue(quality, caseNumber);
  await expect(quality.getByText("Your task:")).toContainText("Approval");
  await quality.getByRole("button", { name: "Sign and complete: Approved" }).click();
  await signWithReauth(quality, "acme.quality", "Sign: Approved");
  await expect(quality.getByText("approved", { exact: true }).first()).toBeVisible();
  return quality.url();
}
