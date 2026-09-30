#!/usr/bin/env node
/**
 * Press-to-start and deck-load latency, Rust engine mode against Web Audio,
 * on the same page, track and dispatcher (NAE-13, perf register Tue 29 Sep 2026).
 *
 * Needs a running backend and SPA; the Rust run starts the engine itself.
 *
 *   BASE=http://127.0.0.1:5273 TRACK=<stable_id> MODE=rust|webaudio \
 *     [ENGINE_CLOCK=wall|device] [N=100] [LOADS=10] node scripts/rust-engine-latency.mjs
 *
 * What each number is (all in page `performance.now()` ms, press = the stamp
 * taken just before `dispatch`):
 * - load: dispatch of `load` to its promise resolving (deck ready to play).
 * - webaudio input_to_audible: the page's own `transport-schedule-press` rows,
 *   press to the scheduled start on the context clock. Excludes the device floor.
 * - rust press_to_result: press to the engine's `result` for `play` arriving on
 *   the page. The engine applies the command on its audio thread right before
 *   rendering the block that starts the deck, then sends the result, so this
 *   is an UPPER bound on press to render start: it also carries the trip back.
 *   Samples leave the engine at most one block (`--block`, 256 frames) later.
 *   Excludes the device floor too.
 *
 * A figure with no samples prints UNMEASURED and the exit code is 1; it is
 * never printed as a number.
 */
import { chromium } from "@playwright/test";

const BASE = (process.env.BASE ?? "http://127.0.0.1:5273").replace(/\/$/, "");
const TRACK = process.env.TRACK;
const MODE = process.env.MODE;
const CLOCK = process.env.ENGINE_CLOCK ?? "device";
const N = Number(process.env.N ?? 100);
const LOADS = Number(process.env.LOADS ?? 10);
if (!TRACK || (MODE !== "rust" && MODE !== "webaudio")) {
  console.error("set TRACK=<stable_id> and MODE=rust|webaudio");
  process.exit(2);
}

const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH || undefined,
  args: ["--autoplay-policy=no-user-gesture-required"],
});
const page = await browser.newPage();
// Timestamp the engine socket's play sends and results on the page's clock.
await page.addInitScript(() => {
  window.__rustLatencyTap = [];
  const send = WebSocket.prototype.send;
  WebSocket.prototype.send = function (data) {
    try {
      const m = JSON.parse(data);
      if (m.cmd?.type === "play")
        window.__rustLatencyTap.push({
          ev: "send",
          id: String(m.id),
          t: performance.now(),
        });
    } catch {
      // not an engine command
    }
    if (!this.__rustLatencyTapped) {
      this.__rustLatencyTapped = true;
      this.addEventListener("message", (e) => {
        try {
          const m = JSON.parse(e.data);
          if (m.type === "result")
            window.__rustLatencyTap.push({
              ev: "result",
              id: String(m.id),
              t: performance.now(),
            });
        } catch {
          // not JSON
        }
      });
    }
    return send.call(this, data);
  };
});
const query =
  MODE === "rust" ? `?engine=rust&engine_clock=${CLOCK}` : "?engine=webaudio";
await page.goto(`${BASE}/performance${query}`, {
  waitUntil: "domcontentloaded",
});
await page.waitForFunction(() => !!window.musicDjToolsPerformance, null, {
  timeout: 60000,
});
if (MODE === "rust") {
  await page.waitForFunction(
    () => document.querySelector('[data-engine-status="connected"]'),
    null,
    { timeout: 30000 },
  );
} else if ((await page.locator("[data-engine-status]").count()) > 0) {
  throw new Error("the Rust engine badge is showing in a Web Audio run");
}

const out = await page.evaluate(
  async ({ track, n, loads }) => {
    const api = window.musicDjToolsPerformance;
    const loadMs = [];
    for (let i = 0; i < loads; i++) {
      await api.dispatch({ type: "unload", deck: 1 }).catch(() => {});
      const t0 = performance.now();
      await api.dispatch({ type: "load", deck: 1, stable_id: track });
      loadMs.push(performance.now() - t0);
    }
    // One unscored play/pause so the first scored press is not a cold start.
    await api.dispatch({ type: "play", deck: 1, playing: true });
    await new Promise((r) => setTimeout(r, 300));
    await api.dispatch({ type: "play", deck: 1, playing: false });
    await new Promise((r) => setTimeout(r, 300));
    const presses = [];
    // The perf log keeps only the newest few press rows, so collect them
    // after every press rather than once at the end.
    const readLog = () =>
      typeof window.__mdtPerfLog === "function" ? window.__mdtPerfLog() : [];
    const rows = new Map();
    for (let i = 0; i < n; i++) {
      const playing = i % 2 === 0;
      const t0 = performance.now();
      await api.dispatch({ type: "play", deck: 1, playing }, t0);
      presses.push({ playing, t0 });
      await new Promise((r) => setTimeout(r, 150 + Math.random() * 100));
      for (const r of readLog()) {
        const key = `${r.t}|${JSON.stringify(r.stages)}`;
        // Rows first seen after a pause press are the pause's; only starts are scored.
        if (r.kind === "transport-schedule-press" && !rows.has(key))
          rows.set(key, playing ? r : null);
      }
    }
    await api.dispatch({ type: "play", deck: 1, playing: false });
    return {
      loadMs,
      presses,
      tap: window.__rustLatencyTap,
      log: [...rows.values()].filter((r) => r !== null),
    };
  },
  { track: TRACK, n: N, loads: LOADS },
);
await browser.close();

let unmeasured = 0;
const pct = (xs, q) =>
  [...xs].sort((a, b) => a - b)[
    Math.min(xs.length - 1, Math.ceil(q * xs.length) - 1)
  ];
function report(label, xs) {
  if (xs.length === 0) {
    unmeasured += 1;
    console.log(`${MODE} ${label}: UNMEASURED (no samples)`);
    return;
  }
  console.log(
    `${MODE} ${label}: n=${xs.length} p50=${pct(xs, 0.5).toFixed(1)} p99=${pct(xs, 0.99).toFixed(1)} max=${Math.max(...xs).toFixed(1)}`,
  );
}

report("load ms", out.loadMs);
if (MODE === "rust") {
  const toResult = [];
  for (const press of out.presses.filter((p) => p.playing)) {
    const sent = out.tap.find((x) => x.ev === "send" && x.t >= press.t0);
    const got =
      sent && out.tap.find((x) => x.ev === "result" && x.id === sent.id);
    if (got) toResult.push(got.t - press.t0);
  }
  report("press_to_result ms (upper bound on press to render start)", toResult);
} else {
  const rows = out.log.filter((r) => r.kind === "transport-schedule-press");
  const stage = (k) => rows.map((r) => r.stages?.[k]).filter(Number.isFinite);
  report(
    "input_to_audible ms (press to scheduled start)",
    stage("input_to_audible_ms"),
  );
  report(
    "device floor ms (base + output latency)",
    rows
      .map((r) => r.stages?.base_latency_ms + r.stages?.output_latency_ms)
      .filter(Number.isFinite),
  );
}
process.exit(unmeasured > 0 ? 1 : 0);
