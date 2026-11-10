/**
 * Beat grid lines pref (issue #10244, WAVEQS-11736), plumbed the same way
 * as tempo-lane-prefs.ts.
 *
 * - beats_bars: every beat line plus stronger downbeat lines (default,
 *   the behaviour before this pref existed).
 * - bars: downbeat lines only (visibleBeatLines at stride 4).
 * - off: no grid lines. Beat sync, quantize and Gothic block snapping
 *   still use the grid; only the painted lines change.
 *
 * The deck overview strip and stem rows paint no grid lines, so the value
 * has nothing to hide there.
 *
 * Not a rune module - the reactive singleton passes its $state object in.
 */
export const WAVE_GRID_LINES_VALUES = ['beats_bars', 'bars', 'off'] as const;
export type WaveGridLines = (typeof WAVE_GRID_LINES_VALUES)[number];

export interface WaveGridLinesPrefsState {
	wave_grid_lines: WaveGridLines;
}

export const WAVE_GRID_LINES_PREF_DEFAULTS: WaveGridLinesPrefsState = {
	wave_grid_lines: 'beats_bars'
};

/** Narrow an unknown to a WaveGridLines, else undefined. */
export function parseWaveGridLines(value: unknown): WaveGridLines | undefined {
	return WAVE_GRID_LINES_VALUES.find((v) => v === value);
}

/** Narrow or throw a TypeError naming the field and the allowed values. */
export function requireWaveGridLines(value: unknown, field: string): WaveGridLines {
	const parsed = parseWaveGridLines(value);
	if (parsed === undefined) {
		throw new TypeError(
			`${field} must be ${WAVE_GRID_LINES_VALUES.join('|')}, got ${String(value)}`
		);
	}
	return parsed;
}

/** Absent key means a blob written before the key existed (caller keeps the
 * default); present but malformed throws with the storage key named. */
export function validateWaveGridLinesPrefFields(
	parsed: { wave_grid_lines?: unknown },
	storageKey: string
): Partial<WaveGridLinesPrefsState> {
	if (parsed.wave_grid_lines === undefined) return {};
	const value = parseWaveGridLines(parsed.wave_grid_lines);
	if (value === undefined) {
		throw new Error(
			`${storageKey}: malformed prefs blob (wave_grid_lines is not ${WAVE_GRID_LINES_VALUES.join('|')}) - ` +
				'clear the localStorage key to recover'
		);
	}
	return { wave_grid_lines: value };
}

export interface WaveGridLinesSetter {
	setWaveGridLines(next: WaveGridLines): void;
}

/** Writes localStorage AND PUTs /api/v1/ui-prefs (agent parity). */
export function makeWaveGridLinesSetter(
	state: WaveGridLinesPrefsState,
	persist: () => void,
	syncDiskPrefs: (patch: Partial<WaveGridLinesPrefsState>) => void
): WaveGridLinesSetter {
	return {
		setWaveGridLines(next: WaveGridLines): void {
			const value = requireWaveGridLines(next, 'wave_grid_lines');
			state.wave_grid_lines = value;
			persist();
			syncDiskPrefs({ wave_grid_lines: value });
		}
	};
}
