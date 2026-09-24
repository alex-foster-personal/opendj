#!/usr/bin/env node
/** Hold Gig or Trackify steady state for PERFMODE-15 mode ratio capture (issue #2701). */

import { createInterface } from "node:readline";
import { createRequire } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

import { watchContinuousPlaybackUntil } from "./trackify-playback-watch.mjs";

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
const mode = values.mode;

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
  await page.goto(`${frontend}/performance?muted=1`, { waitUntil: "domcontentloaded" });
  await waitForPerformanceIpc(page);
  const stableIds = await page.evaluate(async () => {
    const response = await fetch("/api/v1/tracks?limit=4");
    if (!response.ok) throw new Error(`tracks list failed (${response.status})`);
    const payload = await response.json();
    const items = Array.isArray(payload.items) ? payload.items : [];
    if (items.length < 4) throw new Error(`need at least 4 tracks, got ${items.length}`);
    return items.slice(0, 4).map((row) => row.stable_id);
  });
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
  await page.waitForTimeout(5_000);
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
 */
async function openFreshTrackifyBrowser() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto(`${frontend}/music-player?muted=1`, { waitUntil: "domcontentloaded" });
  await waitForTrackifyIpc(page);
  await waitForTrackifyPlaying(page);
  await page.waitForTimeout(5_000);
  return { browser, page };
}

if (mode === "gig-trackify") {
  const gigBrowser = await chromium.launch({ headless: true });
  try {
    const gigContext = await gigBrowser.newContext();
    const gigPage = await gigContext.newPage();
    await loadGigSteadyState(gigPage);
    console.log("GIG_READY");
    await waitForLine();
  } finally {
    await gigBrowser.close();
  }

  const { browser: trackifyBrowser, page: trackifyPage } = await openFreshTrackifyBrowser();
  try {
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
  const { browser: trackifyBrowser, page: trackifyPage } = await openFreshTrackifyBrowser();
  try {
    console.log("TRACKIFY_READY");
    await watchContinuousPlaybackUntil(trackifyPage, waitForLine());
    await waitForTrackifyPlaying(trackifyPage);
    console.log("DONE");
  } finally {
    await trackifyBrowser.close();
  }
}
