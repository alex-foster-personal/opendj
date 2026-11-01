/**
 * App-wide border audit (UIBORDER-01, W27 on issue #5638).
 *
 * the maintainer, Tue 6 Oct 2026: "Check other borders aren't doubling up elsewhere in
 * app - recurring nit". Each doubled line had been fixed one at a time; this
 * walks the real app and flags the class, in both skins:
 *
 * - `double`: two touching elements both painting a border on their shared
 *   edge, so the line reads 2px where its neighbors read 1px.
 * - `clipped`: a border cut off by an `overflow: hidden|clip` ancestor.
 *
 * A hard-edged inset box-shadow (`inset 0 0 0 1px`, Deck.svelte's edge)
 * counts as a border for `double`, since it paints the same line.
 *
 * Surfaces: /performance (decks, mixer and the library browser with a row
 * selected), the Settings overlay scrolled top to bottom, the library page
 * at /, and the /settings route. Skins: Default and Gothic (mono-dev).
 *
 * Intentional or not-yet-fixed cases live in ui-border-audit.allow.json,
 * each with a reason; anything else fails with the selector pair, side and
 * px. The measuring function is support/border-audit.ts.
 *
 * [if] a new doubled or clipped border appears on any audited surface in
 *   either skin [then] stop, naming selector, kind, side and px.
 * control [if] the audit inspects too few elements on a surface (wrong root,
 *   page not rendered) [then] stop: zero findings from an empty measurement
 *   is not a pass.
 * control [if] the planted fixture's known double and clipped borders are not
 *   both reported, or its single shared border and collapsed table are
 *   reported [then] stop: the instrument cannot see what it claims to.
 */
// requirement: UIBORDER-01
import { expect, test, type Page } from "@playwright/test";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
  applyAllowlist,
  auditBorders,
  findingKey,
  formatFindings,
  type BorderAllowEntry,
  type BorderFinding,
} from "./support/border-audit";
import { waitForPerformanceIpc } from "./support/performance-ready";

type Skin = "default" | "mono-dev";
type AllowEntry = BorderAllowEntry & { skins?: Skin[] };
type SurfaceFinding = BorderFinding & { surface: string };

const ALLOW: AllowEntry[] = JSON.parse(
  readFileSync(
    fileURLToPath(new URL("./ui-border-audit.allow.json", import.meta.url)),
    "utf8",
  ),
).entries;

// Tall enough that the library browser shows its header, rows and tray.
test.use({ viewport: { width: 1440, height: 1000 } });

async function applySkin(page: Page, skin: Skin): Promise<void> {
  await page.evaluate((s) => {
    if (s === "default") delete document.documentElement.dataset.skin;
    else document.documentElement.dataset.skin = s;
  }, skin);
  await page.evaluate(
    () =>
      new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))),
  );
}

/** Audits `root`, requiring at least `minInspected` rendered elements. */
async function audit(
  page: Page,
  surface: string,
  root: string,
  minInspected: number,
): Promise<SurfaceFinding[]> {
  const result = await page.evaluate(auditBorders, root);
  expect(
    result.inspected,
    `${surface}: the audit measured too few elements under ${root}`,
  ).toBeGreaterThanOrEqual(minInspected);
  expect(
    result.bordered,
    `${surface}: no bordered element found under ${root}`,
  ).toBeGreaterThan(0);
  return result.findings.map((f) => ({ ...f, surface }));
}

/** Audits a scrolling region top to bottom, one viewport-ish step at a time. */
async function auditScrolled(
  page: Page,
  surface: string,
  root: string,
  scroller: string,
  minInspected: number,
): Promise<SurfaceFinding[]> {
  const found: SurfaceFinding[] = [];
  const box = page.locator(scroller);
  await box.evaluate((el) => (el.scrollTop = 0));
  for (let step = 0; step < 40; step++) {
    found.push(
      ...(await audit(page, `${surface}@${step}`, root, minInspected)),
    );
    const moved = await box.evaluate((el) => {
      const before = el.scrollTop;
      el.scrollTop = before + Math.max(80, el.clientHeight * 0.8);
      return el.scrollTop > before;
    });
    if (!moved) return found;
    await page.evaluate(
      () =>
        new Promise((r) =>
          requestAnimationFrame(() => requestAnimationFrame(r)),
        ),
    );
  }
  throw new Error(`${surface}: ${scroller} never reached its end in 40 steps`);
}

// Real audio from the locked phase7 corpus (ingest-refresh.spec.ts uses the
// same file): upload refuses a file it cannot read a duration from.
const PENDING_FIXTURE = fileURLToPath(
  new URL(
    "../../../../../tests/fixtures/phase7-dedup/src-128.mp3",
    import.meta.url,
  ),
);
const PENDING_FIXTURE_SHA256 =
  "922d6cfa0886ef5a6ae195af01d992782d2680bc40244940254934c9aee7c4d0";

/** The batch this test staged and has not confirmed yet; afterEach confirms
 * it, so a failing audit never leaves an awaiting-Rekordbox batch behind. */
let stagedBatch: string | null = null;

/** Stages one real file through POST /api/v1/ingest/upload. */
async function stagePendingBatch(page: Page, batch: string): Promise<void> {
  const raw = readFileSync(PENDING_FIXTURE);
  expect(
    createHash("sha256").update(raw).digest("hex"),
    "src-128.mp3 changed",
  ).toBe(PENDING_FIXTURE_SHA256);
  const res = await page.request.post("/api/v1/ingest/upload", {
    multipart: {
      batch,
      force: "true",
      files: { name: "border-audit.mp3", mimeType: "audio/mpeg", buffer: raw },
    },
  });
  expect(res.ok(), `ingest upload: ${res.status()} ${await res.text()}`).toBe(
    true,
  );
  stagedBatch = batch;
  const pending = await page.request.get("/api/v1/ingest/pending");
  expect(pending.ok()).toBe(true);
  const names = (
    (await pending.json()) as { batches: { name: string }[] }
  ).batches.map((b) => b.name);
  expect(names, "the staged batch is listed as awaiting Rekordbox").toContain(
    batch,
  );
}

/** Pairs the first two tracks, so deck 2's PAIRED row is always audited. On
 * CI it otherwise shows only because pairing-capture-records.spec.ts left the
 * same pairing behind. The fixture is rebuilt per run, so it is not removed. */
async function stagePairedRow(page: Page): Promise<void> {
  const tracks = await page.request.get("/api/v1/tracks?limit=2");
  expect(tracks.ok()).toBe(true);
  const [from, to] = (
    (await tracks.json()) as { items: { stable_id: string }[] }
  ).items.map((t) => t.stable_id);
  const existing = await page.request.get(
    `/api/v1/pairings?from_stable_id=${from}`,
  );
  expect(existing.ok()).toBe(true);
  if (((await existing.json()) as unknown[]).length > 0) return;
  const created = await page.request.post("/api/v1/pairings", {
    data: {
      from_stable_id: from,
      to_stable_id: to,
      direction: "->",
      source: "manual",
    },
  });
  expect(created.status(), await created.text()).toBe(201);
}

/** The PAIRED row must be fully reachable: scrolled to the end of the strips,
 * nothing of it may sit past the list panel's clip (LIBUX-8186). */
async function expectPairedRowReachable(page: Page): Promise<void> {
  await expect(page.locator(".rec-paired-row").first()).toBeVisible();
  const strips = page.locator(".library-panels-strips");
  const overrunPx = await strips.evaluate((el) => {
    el.scrollTop = el.scrollHeight;
    const row = el.querySelector(".rec-paired-row")!.getBoundingClientRect();
    const panel = el.closest(".list-panel")!.getBoundingClientRect();
    return row.bottom - panel.bottom;
  });
  expect(overrunPx, "PAIRED row bottom past .list-panel").toBeLessThanOrEqual(
    0,
  );
  await strips.evaluate((el) => (el.scrollTop = 0));
}

/** Marks the staged batch imported through the real confirm endpoint. Runs
 * after every test, pass or fail; a cleanup failure is reported on its own
 * and does not replace the test's own result. */
test.afterEach(async ({ request }) => {
  const batch = stagedBatch;
  if (batch === null) return;
  stagedBatch = null;
  const res = await request.post(`/api/v1/ingest/pending/${batch}/confirm`);
  expect(res.status(), `confirm staged batch ${batch}`).toBe(204);
});

function uniqueByKey(findings: SurfaceFinding[]): SurfaceFinding[] {
  const seen = new Map<string, SurfaceFinding>();
  for (const f of findings) {
    const id = `${findingKey(f)}|${f.pair}`;
    if (!seen.has(id)) seen.set(id, f);
  }
  return [...seen.values()];
}

for (const skin of ["default", "mono-dev"] as const) {
  test(`no doubled or clipped borders in the ${skin} skin`, async ({
    page,
  }, testInfo) => {
    test.setTimeout(120_000);
    const findings: SurfaceFinding[] = [];

    // The library-ready card floats over the decks and browser; hidden the
    // way the user hides it, so it does not cover what this audits.
    await page.addInitScript(() =>
      sessionStorage.setItem("odj.enrich-card.hidden", "1"),
    );
    // A real batch awaiting a Rekordbox import, staged through the real
    // upload endpoint, so the bottom bar's amber chip is always audited. On
    // CI it otherwise shows only when an earlier test happened to leave one.
    // One batch per run and skin: confirming writes a marker that hides it.
    const batch = `border-audit-${skin}-${Date.now()}`;
    await stagePendingBatch(page, batch);
    await stagePairedRow(page);
    await page.goto("/performance?muted=1", { waitUntil: "domcontentloaded" });
    await waitForPerformanceIpc(page);
    await page.getByText("All Tracks", { exact: true }).first().click();
    const rows = page.locator('[data-testid="track-row"]');
    await expect(rows.nth(1)).toBeVisible({ timeout: 60_000 });
    await applySkin(page, skin);
    // Loaded decks (waveform, cues, readouts) and a selected row are the
    // bordered states worth covering. Load through the row's own quick-load
    // box, past its 500ms double-click guard (TrackTable.svelte).
    for (const [index, deck] of [
      [0, "1"],
      [1, "2"],
    ] as const) {
      const title = rows.nth(index).locator("td.c-title");
      await title.click();
      await title.hover();
      await page.waitForTimeout(700);
      await rows
        .nth(index)
        .locator("button.deck-target", { hasText: deck })
        .click();
      await expect(
        page.locator(`section.rb-deck[data-deck="${deck}"]`),
      ).toContainText("webkit-fixture", { timeout: 30_000 });
    }
    await expect(page.getByTestId("ingest-awaiting-rb")).toBeVisible({
      timeout: 15_000,
    });
    await page.mouse.move(2, 500);
    await expectPairedRowReachable(page);
    findings.push(...(await audit(page, "performance", "body", 500)));

    await page.keyboard.press("ControlOrMeta+Comma");
    await expect(page.locator(".so-panel")).toBeVisible({ timeout: 15_000 });
    findings.push(
      ...(await auditScrolled(
        page,
        "settings overlay",
        ".so-panel",
        ".so-rhs",
        40,
      )),
    );
    await page.keyboard.press("Escape");

    await page.goto("/", { waitUntil: "domcontentloaded" });
    // "/" is either the classic library table or the Crate Desk (#5729);
    // wait for a real track row in whichever one mounted.
    await expect(
      page
        .getByRole("cell", { name: /webkit-fixture-a/ })
        .or(page.locator('[data-testid="track-row"]'))
        .first(),
    ).toBeVisible({ timeout: 30_000 });
    await applySkin(page, skin);
    findings.push(...(await audit(page, "library", "body", 40)));

    await page.goto("/settings", { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible({
      timeout: 30_000,
    });
    await applySkin(page, skin);
    findings.push(...(await audit(page, "settings page", "body", 40)));

    const unique = uniqueByKey(findings);
    const allow = ALLOW.filter(
      (entry) => !entry.skins || entry.skins.includes(skin),
    );
    const { unexpected, allowed, unusedAllow } = applyAllowlist(unique, allow);
    await testInfo.attach(`border-audit-${skin}.json`, {
      body: JSON.stringify({ skin, findings: unique, unusedAllow }, null, 2),
      contentType: "application/json",
    });
    console.log(`[border-audit ${skin}] allowed:\n${formatFindings(allowed)}`);
    // A stale entry is reported, not failed: it only ever hides that same
    // pair, and failing on it would make the fix PR for an entry red.
    for (const entry of unusedAllow) {
      testInfo.annotations.push({
        type: "warning",
        description: `ui-border-audit.allow.json entry matched nothing in ${skin}; remove it: ${findingKey(entry)}`,
      });
    }
    expect(
      unexpected,
      `new doubled/clipped borders in the ${skin} skin (fix them, or add an entry with a reason to ` +
        `ui-border-audit.allow.json):\n${formatFindings(unexpected)}\n` +
        unexpected.map((f) => `  on ${f.surface}`).join("\n"),
    ).toEqual([]);
  });
}

test("the border audit sees planted doubles and clips, and passes clean edges", async ({
  page,
}) => {
  await page.setViewportSize({ width: 800, height: 600 });
  await page.setContent(`<!doctype html><html><body style="margin:0;background:#111">
		<div id="root" style="padding:20px">
			<!-- double: siblings whose right and left borders touch -->
			<div style="display:flex">
				<div class="dbl-a" style="width:80px;height:30px;border-right:1px solid #888"></div>
				<div class="dbl-b" style="width:80px;height:30px;border-left:1px solid #888"></div>
			</div>
			<!-- double: a child's bottom border on its parent's bottom border -->
			<div class="nest-outer" style="margin-top:20px;border-bottom:1px solid #888">
				<button class="nest-inner" style="display:block;width:60px;height:20px;border:1px solid #888;background:#222"></button>
			</div>
			<!-- double: an inset box-shadow ring (Deck.svelte's edge) on a border -->
			<div style="margin-top:20px">
				<div class="ring-a" style="width:100px;height:30px;box-shadow:inset 0 0 0 1px #888"></div>
				<div class="ring-b" style="width:100px;height:30px;border-top:1px solid #888"></div>
			</div>
			<!-- clipped: a bordered box taller than its overflow:hidden parent -->
			<div class="clipper" style="margin-top:20px;height:20px;overflow:hidden">
				<div class="clipped" style="width:100px;height:30px;border:1px solid #888"></div>
			</div>
			<!-- clean: one shared border, a gap, and a collapsed table -->
			<div style="display:flex;margin-top:20px">
				<div class="ok-a" style="width:80px;height:30px;border-right:1px solid #888"></div>
				<div class="ok-b" style="width:80px;height:30px"></div>
				<div class="ok-c" style="width:80px;height:30px;margin-left:2px;border-left:1px solid #888"></div>
			</div>
			<table class="ok-table" style="border-collapse:collapse;margin-top:20px">
				<tr><td style="border:1px solid #888;width:40px;height:20px"></td><td style="border:1px solid #888;width:40px"></td></tr>
			</table>
			<!-- clean: a border the same color as its own background -->
			<div style="display:flex;margin-top:20px">
				<div class="same-a" style="width:80px;height:30px;background:#333;border-right:2px solid #333"></div>
				<div class="same-b" style="width:80px;height:30px;border-left:1px solid #888"></div>
			</div>
		</div></body></html>`);
  const result = await page.evaluate(auditBorders, "#root");
  const keys = result.findings.map(
    (f) =>
      `${f.kind} ${f.side}/${f.otherSide} ${f.selector} | ${f.other} ${f.px}`,
  );
  expect(keys).toContain(
    "double right/left div#root > div > div.dbl-a | div#root > div > div.dbl-b 2",
  );
  expect(keys).toContain(
    "double bottom/bottom div#root > div.nest-outer | div.nest-outer > button.nest-inner 2",
  );
  expect(keys).toContain(
    "double bottom/top div#root > div > div.ring-a | div#root > div > div.ring-b 2",
  );
  expect(keys).toContain(
    "clipped bottom/bottom div.clipper > div.clipped | div#root > div.clipper 1",
  );
  const clean = result.findings.filter((f) =>
    /ok-|same-/.test(`${f.selector} ${f.other}`),
  );
  expect(clean, formatFindings(clean)).toEqual([]);
  expect(result.findings, formatFindings(result.findings)).toHaveLength(4);
});

test("an allowlist entry covers only as many look-alike pairs as its count", async ({
  page,
}) => {
  await page.setViewportSize({ width: 800, height: 600 });
  // Two controls with the same tag and classes, each sitting on its own
  // row's bottom border: one selector pair, two different element pairs.
  await page.setContent(`<!doctype html><html><body style="margin:0;background:#111">
		<div id="root" style="padding:20px">
			<div class="row" style="border-bottom:1px solid #888;margin-top:20px">
				<button class="twin" style="display:block;width:60px;height:20px;border:1px solid #888;background:#222"></button>
			</div>
			<div class="row" style="border-bottom:1px solid #888;margin-top:20px">
				<button class="twin" style="display:block;width:60px;height:20px;border:1px solid #888;background:#222"></button>
			</div>
		</div></body></html>`);
  const result = await page.evaluate(auditBorders, "#root");
  const twins = result.findings.filter((f) => f.other.endsWith("button.twin"));
  expect(twins, formatFindings(result.findings)).toHaveLength(2);
  expect(findingKey(twins[0])).toBe(findingKey(twins[1]));
  expect(twins[0].pair).not.toBe(twins[1].pair);

  const entry = (count: number): BorderAllowEntry => ({
    kind: "double",
    side: "bottom",
    otherSide: "bottom",
    selector: "div#root > div.row",
    other: "div.row > button.twin",
    count,
    reason: "planted: one twin is intentional",
  });
  expect(findingKey(entry(1))).toBe(findingKey(twins[0]));
  // Allowlisted for one pair, the second look-alike is still flagged.
  const one = applyAllowlist(twins, [entry(1)]);
  expect(one.allowed).toHaveLength(0);
  expect(one.unexpected).toHaveLength(2);
  // Control: the same entry sized for both pairs covers both.
  const both = applyAllowlist(twins, [entry(2)]);
  expect(both.allowed).toHaveLength(2);
  expect(both.unexpected).toHaveLength(0);
  expect(() => applyAllowlist(twins, [entry(0)])).toThrow(/count/);
});
