/** Compatible-filter range prefs (LIBUX-22). */

export type CompatibleBpmDirection = 'both' | 'above' | 'below' | 'same';

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
	bpm_direction: 'both'
};

export function validateCompatibleFilterPrefs(
	raw: unknown,
	storageKey: string
): Partial<CompatibleFilterPrefs> {
	if (raw === undefined) return {};
	if (typeof raw !== 'object' || raw === null) {
		throw new Error(`${storageKey}: compatible_filter must be an object`);
	}
	const obj = raw as Record<string, unknown>;
	const out: Partial<CompatibleFilterPrefs> = {};
	if (obj.camelot_steps !== undefined) {
		if (obj.camelot_steps !== 0 && obj.camelot_steps !== 1 && obj.camelot_steps !== 2) {
			throw new Error(`${storageKey}: compatible_filter.camelot_steps invalid`);
		}
		out.camelot_steps = obj.camelot_steps;
	}
	if (obj.bpm_window_bpm !== undefined) {
		if (typeof obj.bpm_window_bpm !== 'number' || obj.bpm_window_bpm < 0) {
			throw new Error(`${storageKey}: compatible_filter.bpm_window_bpm invalid`);
		}
		out.bpm_window_bpm = obj.bpm_window_bpm;
	}
	if (obj.bpm_enabled !== undefined) {
		if (typeof obj.bpm_enabled !== 'boolean') {
			throw new Error(`${storageKey}: compatible_filter.bpm_enabled invalid`);
		}
		out.bpm_enabled = obj.bpm_enabled;
	}
	if (obj.allow_half_double !== undefined) {
		if (typeof obj.allow_half_double !== 'boolean') {
			throw new Error(`${storageKey}: compatible_filter.allow_half_double invalid`);
		}
		out.allow_half_double = obj.allow_half_double;
	}
	if (obj.bpm_direction !== undefined) {
		const d = obj.bpm_direction;
		if (d !== 'both' && d !== 'above' && d !== 'below' && d !== 'same') {
			throw new Error(`${storageKey}: compatible_filter.bpm_direction invalid`);
		}
		out.bpm_direction = d;
	}
	return out;
}
