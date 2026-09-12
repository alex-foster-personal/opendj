#!/usr/bin/env node
/** Headed Chromium sign-in wait loop for scripts.perf.s13_signin (issue #2113). */

import { chromium } from "@playwright/test";
import { unlinkSync } from "node:fs";
import { parseArgs } from "node:util";

const { values } = parseArgs({
  options: {
    frontend: { type: "string" },
    out: { type: "string" },
    "timeout-s": { type: "string" },
  },
});

const frontend = values.frontend?.replace(/\/$/, "");
const out = values.out;
const timeoutS = Number(values["timeout-s"] ?? "600");

if (!frontend || !out || !Number.isFinite(timeoutS) || timeoutS <= 0) {
  console.error("usage: node s13_signin_browser.mjs --frontend <origin> --out <path> --timeout-s <n>");
  process.exit(1);
}

const headless = process.env.OPENDJ_S13_SIGNIN_HEADLESS === "1";

function removePartial() {
  try {
    unlinkSync(out);
  } catch {
    // absent is fine
  }
}

async function waitForSignedIn(page, deadlineMs) {
  while (Date.now() < deadlineMs) {
    if (page.isClosed()) {
      return false;
    }
    const signedIn = await page.evaluate(async () => {
      const response = await fetch("/api/v1/auth/me", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        return false;
      }
      const payload = await response.json();
      return payload?.signed_in === true;
    });
    if (signedIn) {
      return true;
    }
    await page.waitForTimeout(500);
  }
  return false;
}

let browser;
let finished = false;
try {
  browser = await chromium.launch({ headless });
  browser.on("disconnected", () => {
    if (finished) {
      return;
    }
    console.error("browser closed before sign-in finished");
    removePartial();
    process.exit(1);
  });

  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto(`${frontend}/`, { waitUntil: "domcontentloaded" });

  const deadlineMs = Date.now() + timeoutS * 1000;
  const signedIn = await waitForSignedIn(page, deadlineMs);
  if (!signedIn) {
    if (page.isClosed()) {
      console.error("browser closed before sign-in finished");
    } else {
      console.error("timed out waiting for an authenticated session");
    }
    removePartial();
    process.exit(1);
  }

  await context.storageState({ path: out });
  finished = true;
  await browser.close();
  process.exit(0);
} catch (error) {
  console.error(String(error?.message ?? error));
  removePartial();
  if (browser && !finished) {
    await browser.close().catch(() => {});
  }
  process.exit(1);
}
