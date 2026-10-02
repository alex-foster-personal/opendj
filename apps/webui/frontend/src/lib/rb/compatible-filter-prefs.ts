/** Compatible-filter range prefs (LIBUX-32). */

export type CompatibleBpmDirection = "both" | "above" | "below" | "same";

export interface CompatibleFilterPrefs {
  camelot_steps: 0 | 1 | 2;
  bpm_window_bpm: number;
  bpm_enabled: boolean;
  allow_half_double: boolean;
  bpm_direction: CompatibleBpmDirection;
}

export const COMPATIBLE_FILTER_DEFAULTS: CompatibleFilterPrefs = {
  camelot_steps: 1,
  bpm_window_bpm: 20,
  bpm_enabled: true,
  allow_half_double: true,
  bpm_direction: "both",
};

const isBool = (v: unknown): boolean => typeof v === "boolean";

/** One validity check per field, in the order fields are checked and reported. */
const FIELD_CHECKS: Record<
  keyof CompatibleFilterPrefs,
  (v: unknown) => boolean
> = {
  camelot_steps: (v) => v === 0 || v === 1 || v === 2,
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
  for (const [key, ok] of Object.entries(FIELD_CHECKS)) {
    const value = obj[key];
    if (value === undefined) continue;
    if (!ok(value))
      throw new Error(`${storageKey}: compatible_filter.${key} invalid`);
    out[key] = value;
  }
  return out as Partial<CompatibleFilterPrefs>;
}
