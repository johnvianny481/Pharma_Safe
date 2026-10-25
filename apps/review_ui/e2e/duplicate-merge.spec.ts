/**
 * Duplicate merge: the AI flags a near-duplicate; the reviewer compares the two cases,
 * picks what to keep, e-signs, and the duplicate is closed into the master case.
 */
import { expect, test } from "@playwright/test";

import { report } from "./flows";
import { enterReport, openFromQueue, signIn, signWithReauth, unique, waitForAi } from "./support";

test("a duplicate is merged into its master case", async ({ browser }) => {
  const product = unique("Dupazol");
  const intake = await signIn(browser, "acme.intake");
  const processor = await signIn(browser, "acme.processor");

  const master = await enterReport(intake, report(product, "rash"));
  await openFromQueue(processor, master); // its AI run, and so its case vector, exists now
  const duplicate = await enterReport(intake, report(product, "itching"));
  await openFromQueue(processor, duplicate);
  await waitForAi(processor);

  const panel = processor.getByRole("region", { name: "Possible duplicates" });
  await expect(panel).toContainText(master);
  await expect(panel).toContainText(`same suspect drug: ${product.toLowerCase()}`);
  await panel.getByRole("button", { name: `Compare with ${master}` }).click();

  const compare = processor.getByRole("dialog", { name: `Compare with ${master}` });
  await compare.getByRole("checkbox", { name: `Copy Reaction: itching into ${master}` }).check();
  await compare.getByRole("button", { name: `Merge into ${master}` }).click();
  await signWithReauth(processor, "acme.processor", "Sign: Duplicate merged");

  // Now on the master: the copied reaction arrives as an unconfirmed import.
  await expect(processor.getByRole("heading", { name: new RegExp(`Case ${master}`) })).toBeVisible();
  const reactions = processor.getByRole("region", { name: "Reactions" });
  await expect(reactions.getByLabel("Reported term").nth(1)).toHaveValue("itching");
  await expect(reactions.getByText("Import").first()).toBeVisible();

  // The duplicate is closed and can no longer be changed.
  await processor.goBack();
  await expect(processor.getByRole("status").filter({ hasText: "merged as a duplicate" })).toBeVisible();
  await expect(processor.getByRole("button", { name: /^Confirm/ })).toHaveCount(0);
});
