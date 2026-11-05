// requirement: KEYSHIFT-05
// [if] the pointer rests on the library compatible checkbox [then] the dropdown shows the
//      Mode toggle, every Camelot rule, Key shift, BPM window and BPM direction

import { expect, test } from "@playwright/test";

import { CAMELOT_RULES } from "../../src/lib/player/key/camelot-rules.generated";

const SECTION_HEADINGS = [
  "Mode",
  "Key shift",
  "BPM window",
  "BPM direction",
] as const;

test("compatible filter dropdown renders all its options", async ({ page }) => {
  await page.goto("/performance?muted=1", { waitUntil: "domcontentloaded" });
  await page.waitForFunction(
    () => window.musicDjToolsPerformance?.version === 1,
  );

  const pop = page.locator(".compat-filter-pop").first();
  await expect(pop).toBeVisible();
  await pop.hover();

  // The panel is a lazy chunk loaded on first hover; allow the dev server time to serve it.
  const panel = page.getByRole("group", { name: "Compatible filter options" });
  await expect(panel).toBeVisible({ timeout: 20_000 });

  for (const heading of SECTION_HEADINGS) {
    await expect(
      panel.locator(".compat-filter-heading", { hasText: heading }).first(),
    ).toBeVisible();
  }
  await expect(panel.getByRole("button", { name: "Transition" })).toBeVisible();
  await expect(panel.getByRole("button", { name: "Mashup" })).toBeVisible();

  // Transition mode lists one checkbox per Camelot rule.
  const rules = panel.locator('[data-testid^="compat-rule-"]');
  await expect(rules).toHaveCount(CAMELOT_RULES.length);
  await expect(rules.first()).toBeVisible();

  // Key shift: off and up to three semitones (titles name the shift).
  await expect(panel.locator('button[title="No key shift"]')).toBeVisible();
  await expect(
    panel.locator('button[title^="Match tracks up to"]'),
  ).toHaveCount(3);
});
