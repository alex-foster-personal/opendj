/**
 * SETUP-18 (issue #4040, engine side #3965): a first-run folder import fills
 * the All Tracks table in the page that is ALREADY OPEN, with no reload.
 *
 * The fix (PR #4042) is engine side: the setup-import job publishes
 * `library.changed {kind: "tracks"}` on the events bus when it completes, and
 * the library browser refetches on that event. Before it, the table fetched
 * `/tracks` at boot, when the library was empty, and nothing told it to look
 * again until a browser reload.
 *
 * WHY THIS IS ITS OWN CASE and the happy path's "without a reload" line does
 * not cover it: measured Wed 7 Oct 2026 with the observer registration
 * removed from apps/engine_core/app.py, the happy path stays GREEN, because
 * two OTHER writers publish the same event shortly after the import and mask
 * the missing one:
 *   - the page's once-per-session beatgrid scan (POST /beatgrid-flags/scan,
 *     deferred to the end of the boot window), which publishes when it writes
 *     verdicts for the freshly imported rows;
 *   - auto-analyze, and the wizard's own "Continue" past the import, which
 *     posts /analysis-queue/run, whose ingest refresh publishes on finish.
 * So this case removes each of those from the window under test, honestly:
 *   - the engine runs with MUSIC_DJ_AUTO_ANALYZE=off, a supported setting a
 *     user can choose, under which the import must still refresh the table;
 *   - the beatgrid scan is allowed to run and settle on the EMPTY library
 *     before the import starts (it writes nothing, so publishes nothing);
 *   - the rows are asserted while the wizard still sits on "Import finished",
 *     minimised, BEFORE any "Continue" press.
 * The user selects All Tracks before importing (the #4040 report's step 2),
 * so the table holds the empty boot-time fetch the fix has to invalidate.
 * Nothing is mocked or intercepted; the browser and engine are the real ones.
 *
 * Single-line acceptance checks:
 * - if All Tracks, opened while empty, does not list every imported row within
 *   REFRESH_BUDGET_MS of the job succeeding, with no reload and no further
 *   click on All Tracks -> broken.
 * - if the table lists the imported rows without a reload [then] the sidebar All
 *   Tracks badge reads 3 within the same budget (issue #5862).
 * Proof the BUS supplied the refresh, not the 60 s degraded-path poll:
 *   BrowserPanel's fallback poll stands down while the events bus is open
 *   (app-posture.ts shouldRunLibraryFallbackPoll returns false when busOpen),
 *   and its 60 s interval starts at mount, not at the import, so a time
 *   budget alone cannot rule it out on a slow host (#5860 Codex P2). So the
 *   case watches the page's /api/v1/events WebSocket from before landing and
 *   asserts it never closed before the rows appeared, and that a
 *   `library.changed {kind: "tracks"}` frame arrived after the import began.
 * Control (green with and without the fix): the engine itself serves every
 *   imported row (GET /api/v1/setup/status `tracks`), so a red here is the
 *   open page going stale, never a failed import.
 */
import { expect, test } from "@playwright/test";

import {
  type OnboardingEngine,
  startOnboardingEngine,
  writeMusicFolder,
} from "./support/onboarding-engine";
import {
  checkFolder,
  chooseFolderBranch,
  landAndTimeWizard,
  minimiseWizard,
  readSetupStatus,
  setupDialog,
  startFolderImport,
  waitForTerminalJob,
} from "./support/onboarding-wizard";

const TRACKS = 3;
/** How long after the job succeeds the open page may take to show the rows.
 * Generous for a loaded CI host (the whole case ran in about 12 s locally)
 * and well below the 60 s fallback poll SETUP-18 names, so a pass cannot
 * come from that poll; without the fix the rows stayed at 0 for all of it. */
const REFRESH_BUDGET_MS = 20_000;

let engine: OnboardingEngine;

// The harness, not the case, is POSIX-only: startOnboardingEngine spawns the
// engine through /bin/sh and stops it by SIGKILL to a negative-PID process
// group. No Windows lane runs this config today (windows-parity.yml has no
// Playwright step); the skip names the reason if one ever does.
test.skip(
  process.platform === "win32",
  "startOnboardingEngine spawns /bin/sh and SIGKILLs a POSIX process group",
);

/** What the page's events-bus sockets did, from before landing onward. */
interface BusWatch {
  opened: number;
  closedAt: number[];
  tracksChangedAt: number[];
}

function watchEventsBus(page: import("@playwright/test").Page): BusWatch {
  const watch: BusWatch = { opened: 0, closedAt: [], tracksChangedAt: [] };
  page.on("websocket", (ws) => {
    if (!ws.url().includes("/api/v1/events")) return;
    watch.opened += 1;
    ws.on("close", () => watch.closedAt.push(Date.now()));
    ws.on("framereceived", ({ payload }) => {
      if (typeof payload !== "string") return;
      try {
        const frame = JSON.parse(payload) as {
          topic?: string;
          payload?: { kind?: string };
        };
        if (
          frame.topic === "library.changed" &&
          frame.payload?.kind === "tracks"
        ) {
          watch.tracksChangedAt.push(Date.now());
        }
      } catch {
        // not JSON: not a bus envelope this case reads
      }
    });
  });
  return watch;
}

test.beforeEach(async ({}, testInfo) => {
  engine = await startOnboardingEngine(
    testInfo.title.replace(/\W+/g, "-").slice(0, 40),
    {
      env: { MUSIC_DJ_AUTO_ANALYZE: "off" },
    },
  );
});

test.afterEach(async ({}, testInfo) => {
  if (testInfo.status !== testInfo.expectedStatus) {
    await testInfo.attach("engine.log", {
      body: engine.logTail(),
      contentType: "text/plain",
    });
  }
  await engine.dispose();
});

/** Wait until the engine reports no beatgrid scan running. */
async function waitForBeatgridScanIdle(origin: string): Promise<void> {
  await expect
    .poll(
      async () => {
        // Return the status with the state, so a failing endpoint reads as
        // its HTTP status in the timeout message rather than a bare timeout.
        const response = await fetch(`${origin}/api/v1/beatgrid-flags/scan`);
        if (!response.ok) return `HTTP ${response.status}`;
        return ((await response.json()) as { state: string }).state;
      },
      {
        timeout: 30_000,
        message: "GET beatgrid-flags/scan never reported idle",
      },
    )
    .toBe("idle");
}

test.describe("onboarding gauntlet: the open page sees the import", () => {
  test("a folder import fills the already-open All Tracks table with no reload (#4040)", async ({
    page,
  }) => {
    const music = `${engine.musicRoot}/Fresh Crate`;
    writeMusicFolder(music, TRACKS, "Refresh Probe");

    const bus = watchEventsBus(page);
    const scanRequested = page.waitForResponse(
      (response) =>
        response.url().includes("/api/v1/beatgrid-flags/scan") &&
        response.request().method() === "POST",
      { timeout: 60_000 },
    );
    await landAndTimeWizard(page, engine.origin);
    // A document reload would drop this marker; SPA route changes keep it.
    await page.evaluate(() => {
      (
        window as unknown as { __gauntletNoReload?: boolean }
      ).__gauntletNoReload = true;
    });
    // Let the page's one beatgrid scan run on the empty library first: it
    // would otherwise publish the same event after the import and mask
    // a missing one (see the header).
    expect((await scanRequested).ok()).toBe(true);
    await waitForBeatgridScanIdle(engine.origin);

    // The user opens All Tracks BEFORE importing, as in #4040: minimise the
    // wizard, select All Tracks (empty), then reopen the wizard from its chip.
    const rows = page.locator('[data-testid="track-row"]');
    const allTracks = page.getByTestId("playlist-all-tracks");
    await minimiseWizard(page);
    await allTracks.click();
    await expect(allTracks).toHaveClass(/\bselected\b/);
    await expect(allTracks.locator(".count")).toHaveText("0");
    await expect(rows).toHaveCount(0);
    await page.getByRole("button", { name: /^Setup \(/ }).click();
    await expect(setupDialog(page)).toBeVisible();

    await chooseFolderBranch(page);
    const scan = await checkFolder(page, music);
    expect(scan).toMatchObject({ readable: true, audio_files: TRACKS });
    const importStartedAt = Date.now();
    const jobId = await startFolderImport(page);
    const job = await waitForTerminalJob(engine.origin, jobId);
    expect(job.status, JSON.stringify(job)).toBe("succeeded");

    // Control: the engine serves every row, with or without the fix.
    expect(await readSetupStatus(engine.origin)).toMatchObject({
      library_empty: false,
      tracks: TRACKS,
    });

    // Stay on "Import finished"; never press Continue (its analysis-queue
    // run publishes the same event). Minimise to look at the library.
    await expect(
      setupDialog(page).getByText("Import finished", { exact: false }).first(),
    ).toBeVisible();
    await minimiseWizard(page);

    // No reload, no second click on All Tracks: the open table must fill by itself.
    await expect(rows).toHaveCount(TRACKS, { timeout: REFRESH_BUDGET_MS });
    await expect(allTracks.locator(".count")).toHaveText(String(TRACKS), {
      timeout: REFRESH_BUDGET_MS,
    });
    const rowsShownAt = Date.now();
    // The bus was open the whole time, so the fallback poll stood down: the
    // rows came from the bus fast path (see the header).
    expect(bus.opened, "the page never opened its events bus").toBeGreaterThan(
      0,
    );
    expect(
      bus.closedAt,
      "the events bus closed before the rows appeared; the 60 s poll could have filled them",
    ).toEqual([]);
    expect(
      bus.tracksChangedAt.filter(
        (at) => at >= importStartedAt && at <= rowsShownAt,
      ).length,
      'no library.changed {kind: "tracks"} frame reached the page between the import and the rows',
    ).toBeGreaterThan(0);
    const rowText = await rows.allInnerTexts();
    expect(
      rowText.filter((text) => text.includes("Refresh Probe")).length,
      JSON.stringify(rowText),
    ).toBe(TRACKS);
    const sameDocument = await page.evaluate(
      () =>
        (window as unknown as { __gauntletNoReload?: boolean })
          .__gauntletNoReload === true,
    );
    expect(sameDocument, "the document was reloaded after landing").toBe(true);
  });
});
