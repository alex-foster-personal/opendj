#!/usr/bin/env node
/** Hold Gig or Trackify steady state for PERFMODE-15 mode ratio capture (issue #2701). */

import { createInterface } from "node:readline";
import { createRequire } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

import { selectGigBaselineIds } from "./gig-baseline-tracks.mjs";
import {
  gigBaselineDeckFaults,
  watchContinuousPlaybackUntil,
  watchGigDecksPlayingUntil,
} from "./trackify-playback-watch.mjs";
import { createLineReader, runLeakProtocol } from "./trackify-quiescent-checkpoint.mjs";
import { openUninstrumentedPage } from "./uninstrumented-page.mjs";

async function loadChromium() {
  const resolver = createRequire(path.join(process.cwd(), "package.json"));
  const entry = resolver.resolve("@playwright/test");
  const mod = await import(pathToFileURL(entry).href);
  const { chromium } = mod.default ?? mod;
  if (!chromium) throw new Error("@playwright/test has no chromium export");
  return chromium;
}

const chromium = await loadChromium();

const { values } = parseArgs({
  options: {
    frontend: { type: "string" },
    mode: { type: "string" },
  },
});

const frontend = values.frontend?.replace(/\/$/, "");
const MAX_LISTING_PAGES = 40;
const mode = values.mode;
// PERFMODE-14 protocol: same 60 s settle before each mode dwell as
// library-mode-perf-capture.spec.ts (KPI_CAPTURE_SETTLE_SECONDS default 60).
const SETTLE_S = 60;
const SETTLE_MS = SETTLE_S * 1000;

function emitSettleS() {
  console.log(`SETTLE_S ${SETTLE_S}`);
}

function settleDone() {
  return new Promise((resolve) => {
    setTimeout(resolve, SETTLE_MS);
  });
}

if (!frontend || (mode !== "gig-trackify" && mode !== "trackify-leak")) {
  console.error(
    "usage: node mode_ratio_browser.mjs --frontend <origin> --mode <gig-trackify|trackify-leak>"
  );
  process.exit(1);
}

function waitForLine() {
  return new Promise((resolve) => {
    const rl = createInterface({ input: process.stdin });
    rl.once("line", () => {
      rl.close();
      resolve();
    });
  });
}

async function waitForPerformanceIpc(page) {
  await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
    timeout: 120_000,
  });
}

async function waitForTrackifyIpc(page) {
  await page.waitForFunction(() => window.musicDjToolsTrackify?.version === 1, undefined, {
    timeout: 120_000,
  });
}

/**
 * An idle Trackify page (empty feed, failed hydration, or every candidate
 * quarantined) would otherwise sample as a real "savings" ratio and a flat
 * leak slope, both recorded `measured: true` -- review finding on PR #3676.
 * Wait for an actually-loaded, actually-playing deck before signalling
 * READY, so the capture is over a page that is doing the work it claims.
 */
async function waitForTrackifyPlaying(page) {
  await page.waitForFunction(
    () => {
      const ipc = window.musicDjToolsTrackify;
      if (ipc === undefined) return false;
      const state = ipc.query();
      return state.deck.stable_id !== null && state.deck.playing === true;
    },
    undefined,
    { timeout: 60_000 }
  );
}

async function waitForQueueIdle(page) {
  await page.waitForFunction(
    () => {
      const ipc = window.musicDjToolsPerformance;
      if (ipc === undefined) return false;
      const state = ipc.query();
      return state.command_pending === false && state.command_queued === 0;
    },
    undefined,
    { timeout: 120_000 }
  );
}

async function loadGigSteadyState(page) {
  await page.goto(`${frontend}/performance?muted=1`);
  await waitForPerformanceIpc(page);
  // The first four playable rows are not a Beat Sync gig: phase lock aborts
  // when a follower's BPM is outside [0.84, 1.16] of deck 1, or the track
  // has no beat grid. Page the listing the harness already uses, keep rows
  // whose audio route serves bytes, and choose four tempo-compatible
  // grid-backed tracks. Sync stays engaged — the baseline is a real synced
  // gig, not four free-running decks.
  const listing = await page.evaluate(async (maxPages) => {
    const candidates = [];
    const rejected = [];
    let cursor = null;
    for (let pageIndex = 0; pageIndex < maxPages; pageIndex += 1) {
      const query = cursor === null ? "" : `&cursor=${encodeURIComponent(cursor)}`;
      const response = await fetch(`/api/v1/tracks?limit=50&available=true${query}`);
      if (!response.ok) throw new Error(`tracks list failed (${response.status})`);
      const payload = await response.json();
      const items = Array.isArray(payload.items) ? payload.items : [];
      for (const row of items) {
        if (row.file_exists !== true) continue;
        const audio = await fetch(`/api/v1/tracks/${row.stable_id}/audio`, { method: "HEAD" });
        if (audio.status !== 200) {
          rejected.push(`${String(row.stable_id).slice(0, 8)}=${audio.status}`);
          continue;
        }
        candidates.push({
          stable_id: row.stable_id,
          bpm: row.bpm,
          beatgrid: row.beatgrid ?? null,
          beat_grid: row.beat_grid ?? null,
          has_beatgrid: row.has_beatgrid ?? null,
        });
      }
      cursor = typeof payload.next_cursor === "string" ? payload.next_cursor : null;
      if (cursor === null) break;
    }
    return { candidates, rejected };
  }, MAX_LISTING_PAGES);
  let stableIds;
  try {
    stableIds = selectGigBaselineIds(listing.candidates);
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    const rejected = listing.rejected.length > 0 ? listing.rejected.join(", ") : "none";
    throw new Error(`${detail} (rejected audio: ${rejected})`);
  }
  for (let deck = 1; deck <= 4; deck += 1) {
    const stableId = stableIds[deck - 1];
    await page.evaluate(
      ({ deckId, stable_id }) => {
        const ipc = window.musicDjToolsPerformance;
        if (ipc === undefined) throw new Error("performance IPC is not installed");
        return ipc.dispatch({ type: "load", deck: deckId, stable_id });
      },
      { deckId: deck, stable_id: stableId }
    );
    await waitForQueueIdle(page);
    await page.evaluate(
      ({ deckId }) => {
        const ipc = window.musicDjToolsPerformance;
        if (ipc === undefined) throw new Error("performance IPC is not installed");
        return ipc.dispatch({ type: "play", deck: deckId, playing: true });
      },
      { deckId: deck }
    );
    await waitForQueueIdle(page);
  }
  // Queue-idle and HEAD 200 do not prove a load landed, so before the settle
  // watch require each deck to hold exactly its picked track with a positive
  // duration and be playing (Sol P1/BLOCKING, PR #4540). The settle watch
  // then keeps all four playing through SETTLE_MS; a stall here aborts
  // before SETTLE_S. The whole-window watch after GIG_READY keeps that true
  // while the sampler runs.
  const decks = await page.evaluate((count) => {
    const ipc = window.musicDjToolsPerformance;
    if (ipc === undefined) throw new Error("performance IPC is not installed");
    const all = ipc.query().decks;
    const projected = {};
    for (let deckId = 1; deckId <= count; deckId += 1) {
      const deck = all[deckId];
      projected[deckId] =
        deck === undefined
          ? null
          : { stable_id: deck.stable_id, duration_ms: deck.duration_ms, playing: deck.playing };
    }
    return projected;
  }, stableIds.length);
  const deckFaults = gigBaselineDeckFaults(decks, stableIds);
  if (deckFaults.length > 0) {
    throw new Error(`Gig baseline is not four loaded, playing decks: ${deckFaults.join("; ")}`);
  }
  await watchGigDecksPlayingUntil(page, settleDone(), [1, 2, 3, 4]);
  return stableIds;
}

/**
 * Opens Trackify in a BRAND NEW browser instance rather than navigating the
 * Gig page's own context there. Reusing one Chromium context/page across
 * both samples let the Trackify numerator retain decoded buffers, caches
 * and renderer allocations from the four-deck Gig run, making it
 * history-dependent rather than a real Trackify steady state (Sol review,
 * PR #3676). The launcher's own `proc.pid` (the node process
 * capture_mode_ratios.py samples from) never changes across this handoff,
 * so the process-tree sampler still finds whichever Chromium is currently
 * this process's child at sample time -- no Python-side change needed.
 *
 * Both phases drive an uninstrumented page (`uninstrumented-page.mjs`), never
 * a Playwright `context.newPage()`: Playwright enables the Network domain on
 * its pages, and the renderer then buffers every response body (each track's
 * audio file) for DevTools, up to about 200 MB, which this footprint capture
 * would count as the app's own memory.
 */
async function openFreshTrackifyBrowser() {
  const browser = await chromium.launch({ headless: true });
  const page = await openUninstrumentedPage(browser);
  await page.goto(`${frontend}/music-player?muted=1`);
  await waitForTrackifyIpc(page);
  await waitForTrackifyPlaying(page);
  await watchContinuousPlaybackUntil(page, settleDone());
  return { browser, page };
}

if (mode === "gig-trackify") {
  const gigBrowser = await chromium.launch({ headless: true });
  try {
    const gigPage = await openUninstrumentedPage(gigBrowser);
    const gigStableIds = await loadGigSteadyState(gigPage);
    console.log("GIG_STABLE_IDS " + JSON.stringify(gigStableIds));
    emitSettleS();
    console.log("GIG_READY");
    // GIG_READY only proves the four load/play commands were issued before
    // the wait started; nothing else verifies all four decks are STILL
    // playing throughout the window capture_mode_ratios.py's process-tree
    // sampler actually runs in (a short track ending mid-window would sample
    // a partially idle Gig session as a measured four-deck steady state --
    // Sol review round 6, PR #3676).
    await watchGigDecksPlayingUntil(gigPage, waitForLine(), [1, 2, 3, 4]);
  } finally {
    await gigBrowser.close();
  }

  const { browser: trackifyBrowser, page: trackifyPage } = await openFreshTrackifyBrowser();
  try {
    emitSettleS();
    console.log("TRACKIFY_READY");
    // The sample loop only reads process RSS/CPU, so it cannot itself detect
    // an operator quarantine, a feed running dry, or the page navigating away
    // mid-capture; watch playback throughout the whole window the sampler is
    // running, not only at its two endpoints -- a page that stalls for most
    // of the window and recovers right before "NEXT" arrives must still
    // invalidate the capture (Sol review, PR #3676).
    await watchContinuousPlaybackUntil(trackifyPage, waitForLine());
    await waitForTrackifyPlaying(trackifyPage);
    console.log("DONE");
  } finally {
    await trackifyBrowser.close();
  }
} else {
  // Leak capture: capture_mode_ratios.py interleaves quiescent checkpoints
  // (CHECKPOINT/QUIESCENT/RESUME/RESUMED) with playback, and ends with NEXT
  // (ADR-NEW-trackify-leak-kpi-quiescent-baselines).
  const { browser: trackifyBrowser, page: trackifyPage } = await openFreshTrackifyBrowser();
  const reader = createLineReader(process.stdin);
  try {
    emitSettleS();
    console.log("TRACKIFY_READY");
    await runLeakProtocol(
      trackifyPage,
      () => reader.next(),
      (line) => console.log(line),
      watchContinuousPlaybackUntil
    );
    await waitForTrackifyPlaying(trackifyPage);
    console.log("DONE");
  } finally {
    reader.close();
    await trackifyBrowser.close();
  }
}
