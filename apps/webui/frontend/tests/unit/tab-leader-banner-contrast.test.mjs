/**
 * AGENT-18 / UICONTRAST-01: follower tab-leader banner must use primary ink on
 * panel so the rendered contrast audit clears 4.5:1 without allowlisting.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { before, test } from "node:test";

import { loadTypeScriptModule } from "./load-typescript.mjs";

let cc;

before(async () => {
  cc = await loadTypeScriptModule("src/lib/rb/color-contrast.ts");
});

test("TabLeaderBanner .tab-leader uses var(--rb-text) for the status label", () => {
  const src = readFileSync("src/lib/components/rb/TabLeaderBanner.svelte", "utf8");
  const match = src.match(/\.tab-leader\s*\{([^}]+)\}/);
  assert.ok(match, ".tab-leader block missing");
  const block = match[1];
  assert.match(block, /color:\s*var\(--rb-text\)/, "status label must use primary ink");
  assert.doesNotMatch(block, /color:\s*var\(--rb-text-dim\)/, "dim ink fails composited AA floor");
});

test("shipped rb-text on rb-panel clears 4.5:1 in dark and light perf themes", () => {
  const themeCss = readFileSync("src/lib/rb/theme.css", "utf8");
  for (const selector of [".perf-root", "html[data-theme='light'] .perf-root"]) {
    const tokens = cc.parseColorTokens(themeCss, selector);
    const ratio = cc.contrastRatio(tokens["rb-text"], tokens["rb-panel"]);
    assert.ok(ratio >= 4.5, `${selector}: ${ratio}:1 on panel`);
  }
});
