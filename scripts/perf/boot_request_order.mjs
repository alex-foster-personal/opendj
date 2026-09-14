#!/usr/bin/env node
/** Record PERF-UI-03 boot request order for scripts.perf.s13_signin (issue #2697). */

import { writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

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
    out: { type: "string" },
    "engine-resolved-at": { type: "string" },
  },
});

const frontend = values.frontend?.replace(/\/$/, "");
const out = values.out;
const engineResolvedAt = values["engine-resolved-at"];

if (!frontend || !out || !engineResolvedAt) {
  console.error(
    "usage: node boot_request_order.mjs --frontend <origin> --out <path> --engine-resolved-at <iso>"
  );
  process.exit(1);
}

const events = [{ event: "engine_origin_resolved", at: engineResolvedAt }];
let uiPrefsAt = null;
let tracksAt = null;
let launchAt = null;

const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext();
  const page = await context.newPage();
  page.on("request", (request) => {
    const url = request.url();
    if (uiPrefsAt === null && request.method() === "GET" && url.includes("/api/v1/ui-prefs")) {
      uiPrefsAt = new Date().toISOString();
      events.push({ event: "ui_prefs_get", at: uiPrefsAt });
    }
    if (tracksAt === null && request.method() === "GET" && /\/api\/v1\/tracks/.test(url)) {
      tracksAt = new Date().toISOString();
      events.push({ event: "tracks_get", at: tracksAt });
    }
  });
  await page.addInitScript(() => localStorage.removeItem("odj.brand-launch.v1"));
  await page.goto(`${frontend}/performance`, { waitUntil: "domcontentloaded" });
  const launch = page.getByLabel("Open DJ launch animation");
  await launch.waitFor({ state: "visible", timeout: 10_000 });
  launchAt = new Date().toISOString();
  events.push({ event: "brand_launch_visible", at: launchAt });
  writeFileSync(out, JSON.stringify(events, null, 2), "utf-8");
} finally {
  await browser.close();
}

if (uiPrefsAt === null || tracksAt === null || launchAt === null) {
  console.error("missing one or more request-order markers");
  process.exit(1);
}
if (uiPrefsAt > launchAt || tracksAt > launchAt) {
  console.error("library requests started after BrandLaunch first frame");
  process.exit(2);
}
process.exit(0);
