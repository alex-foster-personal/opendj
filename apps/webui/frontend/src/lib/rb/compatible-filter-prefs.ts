/** Compatible-filter range prefs (LIBUX-32) and Camelot rule checkboxes. */

import {
  DEFAULT_CAMELOT_RULE_IDS,
  isCamelotRuleId,
  type CamelotRuleId,
} from "$lib/player/key/camelot";

export type CompatibleBpmDirection = "both" | "above" | "below" | "same";

export interface CompatibleFilterPrefs {
  /** Enabled rule ids (player/key/camelot-rules.generated.ts). Empty = key not checked. */
  rules: CamelotRuleId[];
  bpm_window_bpm: number;
  bpm_enabled: boolean;
  allow_half_double: boolean;
  bpm_direction: CompatibleBpmDirection;
}

export const COMPATIBLE_FILTER_DEFAULTS: CompatibleFilterPrefs = {
  rules: [...DEFAULT_CAMELOT_RULE_IDS],
  bpm_window_bpm: 20,
  bpm_enabled: true,
  allow_half_double: true,
  bpm_direction: "both",
};

const LEGACY_CAMELOT_STEPS_RULES: Record<0 | 1 | 2, readonly CamelotRuleId[]> = {
  0: ["same_key", "relative"],
  1: DEFAULT_CAMELOT_RULE_IDS,
  2: [...DEFAULT_CAMELOT_RULE_IDS, "energy_boost", "energy_drop"],
};

const isBool = (v: unknown): boolean => typeof v === "boolean";

/** One validity check per field, in the order fields are checked and reported. */
const FIELD_CHECKS: Record<
  keyof CompatibleFilterPrefs,
  (v: unknown) => boolean
> = {
  rules: (v) =>
    Array.isArray(v) &&
    v.every(isCamelotRuleId) &&
    new Set(v).size === v.length,
  bpm_window_bpm: (v) => typeof v === "number" && !(v < 0),
  bpm_enabled: isBool,
  allow_half_double: isBool,
  bpm_direction: (v) =>
    v === "both" || v === "above" || v === "below" || v === "same",
};

export function validateCompatibleFilterPrefs(
  raw: unknown,
  storageKey: string,
): Partial<CompatibleFilterPrefs> {
  if (raw === undefined) return {};
  if (typeof raw !== "object" || raw === null) {
    throw new Error(`${storageKey}: compatible_filter must be an object`);
  }
  const obj = raw as Record<string, unknown>;
  const out: Record<string, unknown> = {};
  // Before rule checkboxes (Tue 27 Oct 2026) the filter stored letter-blind
  // camelot_steps; read it once as the nearest rule set (ui_prefs.py does the same).
  // A legacy value other than 0, 1 or 2 is corrupt and throws, like any field.
  const legacy = obj["camelot_steps"];
  if (obj["rules"] === undefined && legacy !== undefined) {
    if (legacy !== 0 && legacy !== 1 && legacy !== 2)
      throw new Error(`${storageKey}: compatible_filter.camelot_steps invalid`);
    out["rules"] = [...LEGACY_CAMELOT_STEPS_RULES[legacy]];
  }
  for (const [key, ok] of Object.entries(FIELD_CHECKS)) {
    const value = obj[key];
    if (value === undefined) continue;
    if (!ok(value))
      throw new Error(`${storageKey}: compatible_filter.${key} invalid`);
    out[key] = value;
  }
  return out as Partial<CompatibleFilterPrefs>;
}
