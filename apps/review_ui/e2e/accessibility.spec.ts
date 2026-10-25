/**
 * Automated WCAG 2.1 A/AA checks (axe-core) on the main screens. Automated checks find
 * many problems but not all; keyboard and screen-reader reviews complement them.
 */
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

import { report } from "./flows";
import { enterReport, openFromQueue, signIn, unique, waitForAi } from "./support";

async function violations(page: Page): Promise<string[]> {
  const result = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  return result.violations.map((v) => `${v.id}: ${v.help} (${v.nodes.length})`);
}

test("the main screens meet the WCAG 2.1 AA automated checks", async ({ browser }) => {
  const intake = await signIn(browser, "acme.intake");
  await intake.getByRole("link", { name: "New report" }).click();
  expect(await violations(intake)).toEqual([]);
  const caseNumber = await enterReport(intake, report(unique("Axezol")));

  const processor = await signIn(browser, "acme.processor");
  expect(await violations(processor)).toEqual([]); // the work queue
  await openFromQueue(processor, caseNumber);
  await waitForAi(processor);
  expect(await violations(processor)).toEqual([]); // the case workspace
  await processor.getByRole("tab", { name: "History and audit" }).click();
  await expect(processor.getByRole("heading", { name: "Audit trail" })).toBeVisible();
  expect(await violations(processor)).toEqual([]);

  const admin = await signIn(browser, "acme.admin");
  await admin.getByRole("link", { name: "Administration" }).click();
  await expect(admin.getByRole("heading", { name: "Tenant administration" })).toBeVisible();
  expect(await violations(admin)).toEqual([]);
});
