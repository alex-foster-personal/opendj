/**
 * LIBUX-32: reading stored compatible-filter prefs keeps every key the
 * user saved. If validateCompatibleFilterPrefs drops or rewrites a stored key,
 * then the dropdown would show defaults instead of the user's own options.
 */
import assert from "node:assert/strict";
import { before, test } from "node:test";

import { loadTypeScriptModule } from "./load-typescript.mjs";

let mod;

before(async () => {
  mod = await loadTypeScriptModule("src/lib/rb/compatible-filter-prefs.ts");
});

// Every field set away from its default, as a long-used profile stores it.
const STORED = {
  rules: ["same_key", "adjacent", "relative", "diagonal", "pay_attention"],
  bpm_window_bpm: 10,
  bpm_enabled: false,
  allow_half_double: false,
  bpm_direction: "above",
  mode: "mashup",
  key_shift_max: 2,
  mashup_relative: true,
};

test("if reading stored prefs drops or changes a saved key then LIBUX-32 is broken", () => {
  const read = mod.validateCompatibleFilterPrefs(
    structuredClone(STORED),
    "test",
  );
  assert.deepEqual(read, STORED);
  assert.deepEqual(
    Object.keys(read).sort(),
    Object.keys(mod.COMPATIBLE_FILTER_DEFAULTS).sort(),
  );
});

test("if the legacy camelot_steps migration loses the other saved keys then LIBUX-32 is broken", () => {
  const { rules: _rules, ...rest } = STORED;
  const read = mod.validateCompatibleFilterPrefs(
    { ...structuredClone(rest), camelot_steps: 2 },
    "test",
  );
  assert.deepEqual(read, {
    ...rest,
    rules: ["same_key", "adjacent", "relative", "energy_boost", "energy_drop"],
  });
});

test("if a stale camelot_steps overrides a stored rules list then LIBUX-32 is broken", () => {
  const read = mod.validateCompatibleFilterPrefs(
    { ...structuredClone(STORED), camelot_steps: 0 },
    "test",
  );
  assert.deepEqual(read.rules, STORED.rules);
});
