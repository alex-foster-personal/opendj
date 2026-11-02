import { createHash } from "node:crypto";

import { expect, test, type Page, type Route } from "@playwright/test";

import { waitForPerformanceIpc } from "./support/performance-ready";

// requirement: PAIR-09, PAIR-10 (the maintainer, Thu 8 Oct 2026: "Selecting 2 songs
// should have pair these 2 tracks as right click option"; paired rows get a
// purple zigzag edge)
// [if] two library rows are selected and "Pair these 2 tracks" is picked
// [then] the real engine stores a LOOSE pairing (no snapshot) and both rows
// draw the pair edge from the PairingGraph's own GET [else stop].
// [if] the pairing list GET then fails [then] no row draws an edge and a
// "Pairings unavailable" status stays on the table [else stop].
// [if] a later load succeeds [then] the status clears and the edges return.
//
// Everything runs against the real engine daemon and its fixture library.
// The one injected condition is the failure: the browser's GET of the list
// is aborted at the network layer (as midi-setting-disk-on-failed-load does),
// which is a real transport failure through the production fetch path, not a
// fabricated response. Writes are never intercepted.

const ROW = '[data-testid="track-row"]';

interface PairingRow {
  pairing_id: string;
  from_stable_id: string;
  to_stable_id: string;
  direction: string;
  source: string;
  notes: string | null;
  snapshot: unknown;
  updated_at: string;
}

interface Init {
  method: string;
  body?: unknown;
  ifMatch?: string;
}

async function _api<T>(page: Page, path: string, init?: Init): Promise<T> {
  return page.evaluate(
    async ([url, opts]) => {
      const headers: Record<string, string> = {};
      if (opts?.body !== undefined)
        headers["content-type"] = "application/json";
      if (opts?.ifMatch !== undefined) headers["if-match"] = opts.ifMatch;
      const res = await fetch(url, {
        method: opts?.method ?? "GET",
        headers,
        body: opts?.body === undefined ? null : JSON.stringify(opts.body),
      });
      if (!res.ok)
        throw new Error(`${opts?.method ?? "GET"} ${url} -> ${res.status}`);
      return res.status === 204 ? null : res.json();
    },
    [path, init] as const,
  ) as Promise<T>;
}

/** The server's strong validator for a pairing (apps/webui/server/etag.py). */
function _etag(row: PairingRow): string {
  return `"${createHash("sha1").update(`${row.pairing_id}:${row.updated_at}`).digest("hex")}"`;
}

async function _delete(page: Page, row: PairingRow): Promise<void> {
  await _api(page, `/api/v1/pairings/${row.pairing_id}`, {
    method: "DELETE",
    ifMatch: _etag(row),
  });
}

async function _pairingsTouching(
  page: Page,
  ids: readonly string[],
): Promise<PairingRow[]> {
  return (await _api<PairingRow[]>(page, "/api/v1/pairings")).filter(
    (row) => ids.includes(row.from_stable_id) || ids.includes(row.to_stable_id),
  );
}

/** The fixture rows must start unpaired. A test removes only the pairings it
 * created: deleting a pairing it did not create loses its id and timestamps,
 * which a re-POST cannot restore. A fixture that already pairs them fails here
 * and is left untouched. */
/** Two visible rows with no pairing, so a test never has to touch the fixture's own
 * pairings. The fixture is shared and seeded, so its first rows are not guaranteed free. */
async function _twoUnpairedRows(page: Page): Promise<string[]> {
  const paired = new Set(
    (await _api<PairingRow[]>(page, "/api/v1/pairings")).flatMap((row) => [
      row.from_stable_id,
      row.to_stable_id,
    ]),
  );
  // The table is DOM-virtualized, so only the scrolled window is rendered. A
  // seeded fixture can pair every row at the top, so scroll until one window
  // holds two adjacent free rows. Adjacent matters: the test Shift-clicks the
  // second row, and a paired row between them would join the range. Return from
  // that window: its rows are rendered now.
  for (let step = 0; step < 60; step++) {
    const visible = await page
      .locator(ROW)
      .evaluateAll((els) => els.map((el) => el.getAttribute("data-stable-id") ?? ""));
    for (let i = 0; i + 1 < visible.length; i++) {
      const [a, b] = [visible[i], visible[i + 1]];
      if (a && b && a !== b && !paired.has(a) && !paired.has(b)) return [a, b];
    }
    await page.locator(ROW).first().hover();
    await page.mouse.wheel(0, 600);
  }
  throw new Error("the fixture shows at least 2 rows that no pairing touches");
}

async function _requireUnpaired(
  page: Page,
  ids: readonly string[],
): Promise<void> {
  expect(
    await _pairingsTouching(page, ids),
    "the fixture rows start unpaired; the test never removes a pairing it did not create",
  ).toEqual([]);
}

async function _deletePairingsTouching(
  page: Page,
  ids: readonly string[],
): Promise<void> {
  for (const row of await _pairingsTouching(page, ids))
    await _delete(page, row);
}

test("right-click pairs 2 rows loosely; a failed list shows unknown, and a later load recovers", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/performance?muted=1", { waitUntil: "domcontentloaded" });
  await waitForPerformanceIpc(page);

  const rows = page.locator(ROW);
  await expect(
    rows.nth(1),
    "the fixture library shows at least 2 rows",
  ).toBeVisible();
  const ids = await _twoUnpairedRows(page);
  expect(
    ids.every((id) => id !== ""),
    "every row carries its stable id",
  ).toBe(true);
  const [a, b] = ids as [string, string];
  // The test needs both rows unpaired to start, and it removes only the pairings
  // it creates, in the finally below, whether the test passes or fails.
  await _requireUnpaired(page, ids);
  try {
    // Inside the guard: whatever the test created is removed in the finally.
    const rowA = page.locator(`${ROW}[data-stable-id="${a}"]`);
    const rowB = page.locator(`${ROW}[data-stable-id="${b}"]`);
    await expect(rowA).not.toHaveClass(/\brb-row-pair\b/);

    // Record the comet's own start events in the browser before the first
    // pair, so the animation is observed as it runs, not inferred from CSS
    // text. Svelte scopes @keyframes names (s-<hash>-pair-comet), so match the suffix.
    await rowA.evaluate((el) => {
      const stableId = el.getAttribute("data-stable-id");
      const starts: string[] = [];
      (window as unknown as { __pairComet: string[] }).__pairComet = starts;
      document.addEventListener(
        "animationstart",
        (e) => {
          if (
            e.animationName.endsWith("pair-comet") &&
            (e.target as HTMLElement).getAttribute("data-stable-id") ===
              stableId
          ) {
            starts.push(e.pseudoElement);
          }
        },
        true,
      );
    });

    // PAIR-10: select two rows, right-click, "Pair these 2 tracks". Every
    // write below goes through this real flow on the two fixture tracks.
    const pairViaMenu = async (): Promise<void> => {
      await rowA.locator("td.c-title").click();
      await rowB.locator("td.c-title").click({ modifiers: ["Shift"] });
      await rowB.locator("td.c-title").click({ button: "right" });
      const item = page.getByTestId("context-menu-pair");
      await expect(item).toHaveText("Pair these 2 tracks");
      await item.click();
      await expect(item).toHaveCount(0);
    };
    await pairViaMenu();

    await expect
      .poll(async () =>
        (await _api<PairingRow[]>(page, "/api/v1/pairings")).filter(
          (p) =>
            [p.from_stable_id, p.to_stable_id].sort().join() ===
            [a, b].sort().join(),
        ),
      )
      .toHaveLength(1);
    const made = (await _api<PairingRow[]>(page, "/api/v1/pairings")).find(
      (p) =>
        [p.from_stable_id, p.to_stable_id].sort().join() ===
        [a, b].sort().join(),
    )!;
    expect(made.snapshot, "a right-click pair is loose").toBeNull();
    // PAIR-09: the engine's library.changed kind=pairings event refreshes the graph.
    await expect(rowA).toHaveClass(/\brb-row-pair\b/);
    await expect(rowB).toHaveClass(/\brb-row-pair\b/);
    await expect(page.getByTestId("pair-unknown")).toHaveCount(0);

    // The ring must paint, not just carry a class: its pseudo-element is
    // generated, has a box, is masked to the zigzag, and is filled.
    const ring = await rowA.evaluate((el) => {
      const cs = getComputedStyle(el, "::after");
      return {
        content: cs.content,
        width: parseFloat(cs.width),
        height: parseFloat(cs.height),
        mask: cs.maskImage || cs.webkitMaskImage,
        background: cs.backgroundColor,
      };
    });
    expect(ring.content, "the ring pseudo-element is generated").not.toBe(
      "none",
    );
    expect(ring.width, "the ring has a painted width").toBeGreaterThan(0);
    expect(ring.height, "the ring has a painted height").toBeGreaterThan(0);
    expect(ring.mask, "the ring is masked to the zigzag").not.toBe("none");
    expect(ring.background, "the ring is filled").not.toBe("rgba(0, 0, 0, 0)");
    // The comet runs one finite lap: it must have actually started, and end.
    await expect
      .poll(() =>
        rowA.evaluate(
          () => (window as unknown as { __pairComet: string[] }).__pairComet,
        ),
      )
      .toContain("::after");
    await expect
      .poll(() =>
        rowA.evaluate((el) =>
          document.getAnimations().some((a) => {
            // AnimationEffect has no target; only a KeyframeEffect does.
            const effect = a.effect as KeyframeEffect | null;
            return effect?.target === el && effect.pseudoElement === "::after";
          }),
        ),
      )
      .toBe(false);

    // Persistence: the pair is the engine's, not page state. A fresh page
    // load draws both edges again from its own GET of the list.
    await page.reload({ waitUntil: "domcontentloaded" });
    await waitForPerformanceIpc(page);
    await expect(rowA).toHaveClass(/\brb-row-pair\b/);
    await expect(rowB).toHaveClass(/\brb-row-pair\b/);

    // Failure: abort the browser's GET of the list, then pair the same two
    // tracks again. A and B stay paired on the server, so the edges can only
    // clear because the reload the engine's pairings event drives failed.
    const aborted: string[] = [];
    await page.route("**/api/v1/pairings", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      aborted.push(route.request().url());
      return route.abort("failed");
    });
    await pairViaMenu();
    const unknown = page.getByTestId("pair-unknown");
    await expect(unknown).toBeVisible();
    await expect(unknown).toHaveText("Pairings unavailable");
    await expect(unknown).toHaveAttribute("title", /pairing list failed/);
    // Every pushToast lands in the perf-event ring, and the on-screen toast
    // folds and dedupes, so the ring is what counts the toasts that were raised.
    const listFailureToasts = () =>
      page.evaluate(() => {
        const ring = (
          window as unknown as {
            __mdtPerfLog?: () => readonly { kind: string; message: string }[];
          }
        ).__mdtPerfLog;
        if (ring === undefined) return -1;
        return ring().filter(
          (event) =>
            event.kind === "toast-error" &&
            event.message.includes("pairing list failed"),
        ).length;
      });
    // The first failure also reaches the operator as one error toast.
    await expect
      .poll(listFailureToasts, "the first failure toasts once")
      .toBe(1);
    expect(
      aborted.length,
      "the failure came from the aborted GET",
    ).toBeGreaterThan(0);
    await expect(rowA, "a failed load draws no edge").not.toHaveClass(
      /\brb-row-pair\b/,
    );
    await expect(rowB, "a failed load draws no edge").not.toHaveClass(
      /\brb-row-pair\b/,
    );

    // A repeat failure stays quiet: another write reloads the list while it
    // still fails, and the operator sees the first toast only.
    const failedGets = aborted.length;
    await pairViaMenu();
    await expect
      .poll(() => aborted.length, "the repeat reload hit the aborted GET")
      .toBeGreaterThan(failedGets);
    await expect
      .poll(listFailureToasts, "a repeat failure does not toast again")
      .toBe(1);

    // Recovery: stop aborting, and the next real write's event reloads.
    await page.unroute("**/api/v1/pairings");
    await pairViaMenu();
    await expect(unknown).toHaveCount(0);
    await expect(rowA).toHaveClass(/\brb-row-pair\b/);
    await expect(rowB).toHaveClass(/\brb-row-pair\b/);
  } finally {
    await page.unroute("**/api/v1/pairings");
    await _deletePairingsTouching(page, ids);
  }
  expect(await _pairingsTouching(page, ids), "the test leaves no pairing behind").toEqual([]);
});

/** Drives the performance IPC the way the UI does: load and master go through it. */
async function _dispatch(
  page: Page,
  command: Record<string, unknown>,
): Promise<void> {
  await waitForPerformanceIpc(page);
  await page.evaluate(async (message) => {
    const ipc = window.musicDjToolsPerformance;
    if (ipc === undefined) throw new Error("performance IPC is not installed");
    await ipc.dispatch(message);
  }, command);
}

async function _masterDeck(page: Page): Promise<number | null> {
  await waitForPerformanceIpc(page);
  return page.evaluate(
    () => window.musicDjToolsPerformance?.query().master_deck ?? null,
  );
}

// requirement: PAIR-09 (the outage latch in both pairing stores)
// [if] the list GET and both master lookups (as-from, as-to) fail in one outage,
// with the lookups landing later than the list [then] exactly one "pairing ...
// failed" error toast is raised [else stop]. [if] a later success and a new
// failure follow [then] that second outage raises its own toast [else stop].
test("a staggered outage fails both pairing stores and toasts once; the next success re-arms it", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/performance?muted=1", { waitUntil: "domcontentloaded" });
  await waitForPerformanceIpc(page);

  const rows = page.locator(ROW);
  await expect(
    rows.nth(1),
    "the fixture library shows at least 2 rows",
  ).toBeVisible();
  const ids = await _twoUnpairedRows(page);
  expect(
    ids.every((id) => id !== ""),
    "every row carries its stable id",
  ).toBe(true);
  const [a, b] = ids as [string, string];
  const rowA = page.locator(`${ROW}[data-stable-id="${a}"]`);
  const rowB = page.locator(`${ROW}[data-stable-id="${b}"]`);
  await _requireUnpaired(page, ids);
  // Matches the list GET and both lookups (?from_stable_id= / ?to_stable_id=)
  // on the path, whatever their query string.
  const matcher = (url: URL) => url.pathname === "/api/v1/pairings";
  try {
    // The index answers for the MASTER deck only, so elect one: load row A and master it.
    await _dispatch(page, { type: "load", deck: 1, stable_id: a });
    await _dispatch(page, { type: "master", deck: 1 });
    await expect.poll(() => _masterDeck(page)).toBe(1);

    const pairViaMenu = async (): Promise<void> => {
      await rowA.locator("td.c-title").click();
      await rowB.locator("td.c-title").click({ modifiers: ["Shift"] });
      await rowB.locator("td.c-title").click({ button: "right" });
      const item = page.getByTestId("context-menu-pair");
      await expect(item).toHaveText("Pair these 2 tracks");
      await item.click();
      await expect(item).toHaveCount(0);
    };
    // Every pairing toast lands in the perf-event ring, the one counted here.
    const outageToasts = () =>
      page.evaluate(() => {
        const ring = (
          window as unknown as {
            __mdtPerfLog?: () => readonly { kind: string; message: string }[];
          }
        ).__mdtPerfLog;
        if (ring === undefined) return -1;
        return ring().filter(
          (event) =>
            event.kind === "toast-error" &&
            /^pairing (list|lookup) failed/.test(event.message),
        ).length;
      });
    // Each failing GET is recorded; the lookups are held back so the list's
    // failure lands first, and the stores are both in flight during the outage.
    const aborted: { filtered: boolean }[] = [];
    const failStaggered = async (route: Route): Promise<void> => {
      if (route.request().method() !== "GET") return route.continue();
      const url = new URL(route.request().url());
      const filtered =
        url.searchParams.has("from_stable_id") ||
        url.searchParams.has("to_stable_id");
      await new Promise((resolve) => setTimeout(resolve, filtered ? 900 : 200));
      // Counted only once the abort lands: a request the page settled during the
      // delay did not fail, so it must not satisfy the waits below.
      const landed = await route.abort("failed").then(
        () => true,
        () => false,
      );
      if (landed) aborted.push({ filtered });
    };
    const filteredFailures = () => aborted.filter((f) => f.filtered).length;

    // Outage one: the list fails first, both lookups fail after it.
    await page.route(matcher, failStaggered);
    await pairViaMenu();
    await expect
      .poll(filteredFailures, "both master lookups hit the aborted GET")
      .toBeGreaterThanOrEqual(2);
    await expect
      .poll(outageToasts, "the first outage toasts exactly once")
      .toBe(1);
    // Let the second store's failure settle; the latch must still hold at one.
    await expect
      .poll(() => aborted.length, "the list and both lookups all failed")
      .toBeGreaterThanOrEqual(3);
    expect(await outageToasts(), "one outage, one error toast").toBe(1);

    // Recovery: stop aborting. The next write reloads both stores; the latch re-arms.
    await page.unroute(matcher);
    await pairViaMenu();
    await expect(page.getByTestId("pair-unknown")).toHaveCount(0);
    await expect(rowA).toHaveClass(/\brb-row-pair\b/);

    // Outage two, after a full settle: the latch re-armed, so it toasts again.
    await page.route(matcher, failStaggered);
    const failuresBeforeSecond = aborted.length;
    await pairViaMenu();
    await expect
      .poll(() => aborted.length, "the second write reloaded the stores")
      .toBeGreaterThan(failuresBeforeSecond);
    await expect
      .poll(outageToasts, "a second outage after recovery toasts again")
      .toBe(2);
    await page.unroute(matcher);
  } finally {
    await page.unroute(matcher);
    await _deletePairingsTouching(page, ids);
  }
  expect(await _pairingsTouching(page, ids), "the test leaves no pairing behind").toEqual([]);
});

test("a failed partner lookup alone keeps the pairings-unavailable status up", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/performance?muted=1", { waitUntil: "domcontentloaded" });
  await waitForPerformanceIpc(page);

  const rows = page.locator(ROW);
  await expect(rows.nth(1), "the fixture library shows at least 2 rows").toBeVisible();
  const ids = await _twoUnpairedRows(page);
  const [a] = ids as [string, string];
  // Only the index's filtered lookups fail; the graph's unfiltered list succeeds.
  // The index alone then holds the outage, and the status must say so.
  const matcher = (url: URL) => url.pathname === "/api/v1/pairings";
  const failLookups = async (route: Route): Promise<void> => {
    if (route.request().method() !== "GET") return route.continue();
    const url = new URL(route.request().url());
    const filtered =
      url.searchParams.has("from_stable_id") ||
      url.searchParams.has("to_stable_id");
    return filtered ? route.abort("failed") : route.continue();
  };
  await page.route(matcher, failLookups);
  try {
    // The index answers for the MASTER deck only, so electing one starts its lookups.
    await _dispatch(page, { type: "load", deck: 1, stable_id: a });
    await _dispatch(page, { type: "master", deck: 1 });
    await expect.poll(() => _masterDeck(page)).toBe(1);

    const unknown = page.getByTestId("pair-unknown");
    await expect(
      unknown,
      "a failed lookup shows the persistent pairings-unavailable status",
    ).toBeVisible();
    await expect(unknown).toHaveAttribute("title", /pairing lookup failed/);
  } finally {
    await page.unroute(matcher);
  }
});

test("a failed create toasts the error and never the success line", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/performance?muted=1", { waitUntil: "domcontentloaded" });
  await waitForPerformanceIpc(page);

  const rows = page.locator(ROW);
  await expect(rows.nth(1), "the fixture library shows at least 2 rows").toBeVisible();
  const ids = await _twoUnpairedRows(page);
  const [a, b] = ids as [string, string];
  await _requireUnpaired(page, ids);
  try {
    const rowA = page.locator(`${ROW}[data-stable-id="${a}"]`);
    const rowB = page.locator(`${ROW}[data-stable-id="${b}"]`);

    // The browser's POST never reaches the engine: a real transport failure
    // through the production createPairing path, as the list GET test does.
    const aborted: string[] = [];
    await page.route("**/api/v1/pairings", (route) => {
      if (route.request().method() !== "POST") return route.continue();
      aborted.push(route.request().url());
      return route.abort("failed");
    });
    await rowA.locator("td.c-title").click();
    await rowB.locator("td.c-title").click({ modifiers: ["Shift"] });
    await rowB.locator("td.c-title").click({ button: "right" });
    const item = page.getByTestId("context-menu-pair");
    await expect(item).toHaveText("Pair these 2 tracks");
    await item.click();
    await expect(item).toHaveCount(0);

    await expect
      .poll(() => aborted.length, "the create reached the aborted POST")
      .toBeGreaterThan(0);
    const toasts = () =>
      page.evaluate(() => {
        const ring = (
          window as unknown as {
            __mdtPerfLog?: () => readonly { kind: string; message: string }[];
          }
        ).__mdtPerfLog;
        return ring === undefined ? [] : ring().filter((e) => e.kind === "toast-error" || e.message.startsWith("Paired"));
      });
    await expect
      .poll(async () => (await toasts()).some((e) => e.kind === "toast-error"), "the failed create raises an error toast")
      .toBe(true);
    expect(
      (await toasts()).some((e) => e.message.startsWith("Paired")),
      "a failed create never shows the success line",
    ).toBe(false);
    expect(
      (await _pairingsTouching(page, ids)).length,
      "a failed create leaves no pairing behind",
    ).toBe(0);
    await page.unroute("**/api/v1/pairings");
  } finally {
    await page.unroute("**/api/v1/pairings");
    await _deletePairingsTouching(page, ids);
  }
  expect(await _pairingsTouching(page, ids), "a failed create leaves the pairings that touched these rows exactly as they were").toEqual([]);
});
