/**
 * ANALYSIS-11724: per-lane analyze-on-import toggles persisted in ui-prefs.
 */
export interface AutoAnalyzePrefsState {
	auto_analyze_grid: boolean;
	auto_analyze_key: boolean;
	auto_analyze_sections: boolean;
	auto_analyze_vocals: boolean;
}

export const AUTO_ANALYZE_PREF_DEFAULTS: AutoAnalyzePrefsState = {
	auto_analyze_grid: true,
	auto_analyze_key: true,
	auto_analyze_sections: true,
	auto_analyze_vocals: false
};

type AutoAnalyzeKey = keyof AutoAnalyzePrefsState;

export function validateAutoAnalyzePrefFields(
	parsed: Partial<Record<AutoAnalyzeKey, unknown>>,
	storageKey: string
): Partial<AutoAnalyzePrefsState> {
	const out: Partial<AutoAnalyzePrefsState> = {};
	for (const key of Object.keys(AUTO_ANALYZE_PREF_DEFAULTS) as AutoAnalyzeKey[]) {
		const value = parsed[key];
		if (value === undefined) continue;
		if (typeof value !== 'boolean') {
			throw new Error(
				`${storageKey}: malformed prefs blob (${key} is not a boolean) - clear the key to recover`
			);
		}
		out[key] = value;
	}
	return out;
}

export type AutoAnalyzePatch = Partial<AutoAnalyzePrefsState>;

export interface AutoAnalyzeSetters {
	setAutoAnalyzeGrid(next: boolean): void;
	setAutoAnalyzeKey(next: boolean): void;
	setAutoAnalyzeSections(next: boolean): void;
	setAutoAnalyzeVocals(next: boolean): void;
}

export function makeAutoAnalyzeSetters(
	state: AutoAnalyzePrefsState,
	persist: () => void,
	syncDiskPrefs: (patch: AutoAnalyzePatch) => void
): AutoAnalyzeSetters {
	function set<K extends AutoAnalyzeKey>(key: K, next: boolean): void {
		state[key] = next;
		persist();
		syncDiskPrefs({ [key]: next });
	}
	return {
		setAutoAnalyzeGrid: (next) => set('auto_analyze_grid', next),
		setAutoAnalyzeKey: (next) => set('auto_analyze_key', next),
		setAutoAnalyzeSections: (next) => set('auto_analyze_sections', next),
		setAutoAnalyzeVocals: (next) => set('auto_analyze_vocals', next)
	};
}
