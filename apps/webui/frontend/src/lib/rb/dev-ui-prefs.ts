/**
 * "Show developer pages" UI pref (V1 polish, JIK Thu 1 Oct 2026: hide unbuilt
 * and developer-only UI by default).
 *
 * One flag, default OFF, that reveals:
 *   - the sidebar's developer links (Admin, the progress ledger at
 *     /progress-tree, Queues) in routes/+layout.svelte;
 *   - the unbuilt PARITY-TODO settings rows and their "(todo)" groups in the
 *     settings overlay (lib/settings/search.ts `showDev`).
 *
 * Persisted LOCALLY only (the rb-ui-prefs localStorage blob, like
 * horizontal_wheel_knob): it is a per-browser developer convenience, not a
 * library preference, so it is never written to the server's ui-prefs file.
 */

export interface DevUiPrefs {
	show_dev_ui: boolean;
}

export const DEV_UI_PREF_DEFAULTS: DevUiPrefs = {
	show_dev_ui: false
};

export function mergeDevUiPrefsFromParsed(
	parsed: Partial<Record<keyof DevUiPrefs, unknown>>,
	storageKey: string
): DevUiPrefs {
	const value = parsed.show_dev_ui;
	if (value === undefined) return { ...DEV_UI_PREF_DEFAULTS };
	if (typeof value !== 'boolean') {
		throw new Error(
			`${storageKey}: malformed prefs blob (show_dev_ui is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	return { show_dev_ui: value };
}

export function makeDevUiPrefSetters(
	state: DevUiPrefs,
	persist: () => void
): { setShowDevUi(next: boolean): void } {
	return {
		setShowDevUi(next: boolean): void {
			if (typeof next !== 'boolean') {
				throw new Error(`show_dev_ui must be a boolean, got ${String(next)}`);
			}
			state.show_dev_ui = next;
			persist();
		}
	};
}
