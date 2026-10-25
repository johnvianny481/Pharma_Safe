/** Helpers for the end-to-end tests: real Keycloak sign-in, intake, and the workspace. */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { expect, type Browser, type Page } from "@playwright/test";

function demoPassword(): string {
  if (process.env.KC_DEMO_USER_PASSWORD) return process.env.KC_DEMO_USER_PASSWORD;
  const env = readFileSync(fileURLToPath(new URL("../../../.env", import.meta.url)), "utf8");
  const line = env.split(/\r?\n/).find((l) => l.startsWith("KC_DEMO_USER_PASSWORD="));
  if (!line) throw new Error("KC_DEMO_USER_PASSWORD is not set (environment or .env)");
  return line.slice("KC_DEMO_USER_PASSWORD=".length).trim();
}

export const PASSWORD = demoPassword();

/** A unique, letters-only word (it reads like a product name), so each test finds its own cases. */
export function unique(prefix: string): string {
  const n = `${Date.now()}${Math.floor(Math.random() * 1e4)}`;
  return prefix + [...n].map((d) => "abcdefghij"[Number(d)]).join("");
}

async function keycloakLogin(page: Page, username: string): Promise<void> {
  await page.locator("#password").waitFor();
  // On re-authentication (prompt=login) Keycloak shows the username fixed; only the
  // password is asked for again.
  const user = page.locator("input#username");
  if ((await user.count()) > 0 && (await user.isEditable())) await user.fill(username);
  await page.locator("#password").fill(PASSWORD);
  await page.locator("#kc-login").click();
}

/** A fresh browser context signed in as one of the demo users. */
export async function signIn(browser: Browser, username: string): Promise<Page> {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await keycloakLogin(page, username);
  await expect(page.getByRole("heading", { name: "Work queue" })).toBeVisible();
  return page;
}

/**
 * Press a signing button, re-authenticate in the Keycloak popup, and wait for the
 * signature dialog to close.
 */
export async function signWithReauth(page: Page, username: string, signButton: string) {
  const popup = page.waitForEvent("popup");
  await page.getByRole("button", { name: signButton }).click();
  const login = await popup;
  await keycloakLogin(login, username);
  await expect(page.getByRole("dialog", { name: "Electronic signature" })).toBeHidden();
}

export interface Report {
  initials: string;
  product: string;
  reaction: string;
  description: string;
}

/** Enter a report on the intake form; returns the new case number. */
export async function enterReport(page: Page, report: Report): Promise<string> {
  await page.getByRole("link", { name: "New report" }).click();
  const another = page.getByRole("button", { name: "Enter another report" });
  if (await another.isVisible()) await another.click();
  await page.getByLabel("Family name").fill("Silva");
  await page.getByLabel("Qualification").selectOption("physician");
  await page.getByLabel("Initials").fill(report.initials);
  await page.getByLabel("Sex").selectOption("female");
  await page.getByLabel("Suspected medicine").fill(report.product);
  await page.getByLabel("Reaction").fill(report.reaction);
  await page.getByLabel(/Onset date/).fill("2026-10-01");
  await page.getByLabel(/Country/).fill("US");
  await page.getByLabel("Description").fill(report.description);
  await page.getByRole("button", { name: "Submit report" }).click();
  const status = page.getByRole("status").filter({ hasText: "Created case" });
  await expect(status).toBeVisible();
  const match = /Created case (\S+)\./.exec((await status.textContent()) ?? "");
  if (!match?.[1]) throw new Error("no case number in the acknowledgment");
  return match[1];
}

/** Open a case from the work queue (it appears once the workflow has opened its task). */
export async function openFromQueue(page: Page, caseNumber: string): Promise<void> {
  await page.getByRole("link", { name: "Work queue" }).click();
  const link = page.getByRole("link", { name: caseNumber, exact: true });
  // This page is in a background browser context while others act, so its polling
  // may be paused (as in a background tab); reload until the workflow's task shows.
  await expect(async () => {
    await page.reload();
    await expect(link).toBeVisible({ timeout: 5_000 });
  }).toPass({ timeout: 120_000 });
  await link.click();
  await expect(page.getByRole("heading", { name: new RegExp(`Case ${caseNumber}`) })).toBeVisible();
}

/** Wait until the AI pipeline has recorded its run for this version. */
export async function waitForAi(page: Page): Promise<void> {
  await expect(page.getByRole("heading", { name: "AI processing" })).toBeVisible({ timeout: 90_000 });
}

/** Assess every seriousness criterion of every reaction: "No" unless listed as "Yes". */
export async function assessSeriousness(page: Page, yes: string[] = []): Promise<void> {
  const groups = page.getByRole("radiogroup");
  const count = await groups.count();
  for (let i = 0; i < count; i++) {
    const group = groups.nth(i);
    const name = (await group.locator(".react-aria-Label").textContent()) ?? "";
    const answer = yes.includes(name) ? "Yes" : "No";
    const saved = page.waitForResponse((r) => r.request().method() === "PATCH");
    await group.getByText(answer, { exact: true }).click();
    expect((await saved).ok()).toBe(true);
    await expect(group.getByRole("radio", { name: answer, exact: true })).toBeChecked();
  }
}

/** Confirm every unconfirmed value, section by section, then any left one by one. */
export async function confirmEverything(page: Page): Promise<void> {
  for (const buttons of [
    page.getByRole("button", { name: /^Confirm all \d+ in / }),
    page.getByRole("button", { name: /^Confirm / }),
  ]) {
    for (let round = 0; round < 60; round++) {
      const before = await buttons.count();
      if (before === 0) break;
      const saved = page.waitForResponse((r) => r.request().method() === "PATCH");
      await buttons.first().click();
      expect((await saved).ok()).toBe(true);
      // Wait for the refreshed case before looking for the next button.
      await expect.poll(() => buttons.count()).toBeLessThan(before);
    }
  }
  await expect(page.getByText(/things to resolve before approval/)).toBeHidden();
}
