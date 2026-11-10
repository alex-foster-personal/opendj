/** Compatible-filter range prefs (LIBUX-32), Camelot rule checkboxes, and the
 * Transition / Mashup modes with the allowed key shift (KEYSHIFT-01..05). */

import {
  DEFAULT_CAMELOT_RULE_IDS,
  isCamelotRuleId,
  type CamelotRuleId,
  type CompatibleKeyRule,
} from "$lib/player/key/camelot";

export type CompatibleBpmDirection = "both" | "above" | "below" | "same";
export type CompatibleMode = "transition" | "mashup";
export type CompatibleKeyShiftMax = 0 | 1 | 2 | 3;
/** SETPREP-11947: the energy rule, after Mixed In Key's Energy Boost
 * convention (mimicked). `off` checks no energy. */
export type CompatibleEnergyMode = "off" | "within_1" | "boost" | "drop";
export const COMPATIBLE_ENERGY_MODES: readonly CompatibleEnergyMode[] = [
  "off",
  "within_1",
  "boost",
  "drop",
];
export function isCompatibleEnergyMode(v: unknown): v is CompatibleEnergyMode {
  return v === "off" || v === "within_1" || v === "boost" || v === "drop";
}

export interface CompatibleFilterPrefs {
  /** Enabled rule ids (player/key/camelot-rules.generated.ts). Empty = key not checked. */
  rules: CamelotRuleId[];
  bpm_window_bpm: number;
  bpm_enabled: boolean;
  allow_half_double: boolean;
  bpm_direction: CompatibleBpmDirection;
  /** Transition uses `rules`; Mashup is the same wheel number (KEYSHIFT-01). */
  mode: CompatibleMode;
  key_shift_max: CompatibleKeyShiftMax;
  mashup_relative: boolean;
  /** SETPREP-11947: within one MIK energy level of the reference track, boost
   * (+1 to +2) or drop (-1 to -2). Absent acts as off. */
  energy_mode: CompatibleEnergyMode;
}

export const COMPATIBLE_FILTER_DEFAULTS: CompatibleFilterPrefs = {
  rules: [...DEFAULT_CAMELOT_RULE_IDS],
  bpm_window_bpm: 20,
  bpm_enabled: true,
  allow_half_double: true,
  bpm_direction: "both",
  mode: "transition",
  key_shift_max: 1,
  mashup_relative: false,
  energy_mode: "off",
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
  mode: (v) => v === "transition" || v === "mashup",
  key_shift_max: (v) => v === 0 || v === 1 || v === 2 || v === 3,
  mashup_relative: isBool,
  energy_mode: isCompatibleEnergyMode,
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

/** Whether the key is checked at all: Mashup always checks it; Transition
 * checks it only while at least one Camelot rule is ticked. */
export function compatibleKeyChecked(prefs: CompatibleFilterPrefs): boolean {
  return prefs.mode === "mashup" || prefs.rules.length > 0;
}

/** The key rule the active mode applies (KEYSHIFT-01). Transition is the
 * ticked Camelot rules; Mashup is always the same wheel number. */
export function compatibleKeyRule(prefs: CompatibleFilterPrefs): CompatibleKeyRule {
  return prefs.mode === "mashup"
    ? { mode: "mashup", relative: prefs.mashup_relative }
    : { mode: "transition", rules: prefs.rules };
}
