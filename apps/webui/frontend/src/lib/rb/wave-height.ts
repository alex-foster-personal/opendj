/** WAVEQS-11733: deck waveform drawn mirrored (full) or single-sided from the bottom baseline (half). */
export const WAVE_HEIGHTS = ['full', 'half'] as const;
export type WaveHeight = (typeof WAVE_HEIGHTS)[number];
export const WAVE_HEIGHT_DEFAULT: WaveHeight = 'full';

export const WAVE_HEIGHT_PREF_DEFAULTS = { wave_height: WAVE_HEIGHT_DEFAULT } as const;

/** Narrow an unknown to a WaveHeight, else undefined. */
export function parseWaveHeight(value: unknown): WaveHeight | undefined {
	return WAVE_HEIGHTS.find((height) => height === value);
}

export function validateWaveHeightPrefFields(
	parsed: { wave_height?: unknown },
	storageKey: string
): Partial<{ wave_height: WaveHeight }> {
	if (parsed.wave_height === undefined) return {};
	const height = parseWaveHeight(parsed.wave_height);
	if (height === undefined) {
		throw new Error(
			`${storageKey}: malformed prefs blob (wave_height is not ${WAVE_HEIGHTS.join('|')}) - ` +
				'clear the localStorage key to recover'
		);
	}
	return { wave_height: height };
}
