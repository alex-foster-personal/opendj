/**
 * Top-bar view preferences (TOPVIEW-01..06): which view the /performance
 * wavestack area shows, whether the performance-pad rows are shown, and
 * whether AutoPlay keeps to decks 1 and 2. Types, validators and setters,
 * split out of prefs.svelte.ts the way deck-layout-prefs.ts is.
 *
 * Deliberately NOT a rune module (no $state here): the reactive singleton in
 * prefs.svelte.ts owns the state and passes it in by reference.
 */

import { isPerfViewId, PERF_VIEW_IDS, parsePerfViewId, type PerfViewId } from './view-registry';

/**
 * What the wavestack area of /performance shows (TOPVIEW-01..03). Allowed ids
 * come from view-registry.ts so agents add one row instead of parallel lists.
 */
export const PERF_VIEWS = PERF_VIEW_IDS;
export type PerfView = PerfViewId;

export function isPerfView(value: unknown): value is PerfView {
	return isPerfViewId(value);
}

/** Fail-fast: an unlisted view is a bug in the caller, not a silent default. */
export function assertPerfView(value: unknown): asserts value is PerfView {
	parsePerfViewId(value);
}

/** Validate the top-bar view fields of a parsed prefs blob (all optional). */
export function validateTopbarViewFields(
	parsed: { perf_view?: unknown; show_pads?: unknown; auto_play_two_deck?: unknown },
	storageKey: string
): {
	perf_view: PerfView | undefined;
	show_pads: boolean | undefined;
	auto_play_two_deck: boolean | undefined;
} {
	if (parsed.perf_view !== undefined && !isPerfView(parsed.perf_view)) {
		throw new Error(
			`${storageKey}: malformed prefs blob (perf_view must be ${PERF_VIEWS.join('|')}) - ` +
				'clear the localStorage key to recover'
		);
	}
	for (const key of ['show_pads', 'auto_play_two_deck'] as const) {
		if (parsed[key] !== undefined && typeof parsed[key] !== 'boolean') {
			throw new Error(
				`${storageKey}: malformed prefs blob (${key} is not a boolean) - ` +
					'clear the localStorage key to recover'
			);
		}
	}
	return {
		perf_view: parsed.perf_view as PerfView | undefined,
		show_pads: parsed.show_pads as boolean | undefined,
		auto_play_two_deck: parsed.auto_play_two_deck as boolean | undefined
	};
}

/** The slice of RbUiPrefs the setters below read and write. */
export interface TopbarViewPrefsState {
	perf_view: PerfView;
	show_pads: boolean;
	auto_play_two_deck: boolean;
}

export const TOPBAR_VIEW_DEFAULTS: TopbarViewPrefsState = {
	perf_view: 'waveform',
	show_pads: true,
	auto_play_two_deck: false
};

export interface TopbarViewSetters {
	setPerfView(next: PerfView): void;
	setShowPads(next: boolean): void;
	setAutoPlayTwoDeck(next: boolean): void;
}

/**
 * Copy the top-bar view fields a disk prefs body carries onto `state`.
 *
 * Fail-fast (Sol P1 4284394487): the whole payload is validated first with
 * validateTopbarViewFields, and a malformed value (an unknown perf_view, a
 * non-boolean show_pads or auto_play_two_deck) throws before ANY field is
 * written, so a half-applied body can never land. The caller (prefs-hydrate)
 * reports the throw on the settings error sink; it must never swallow it.
 */
export function hydrateTopbarViewFromDisk(
	state: TopbarViewPrefsState,
	body: { perf_view?: unknown; show_pads?: unknown; auto_play_two_deck?: unknown }
): void {
	const v = validateTopbarViewFields(body, 'GET /api/v1/ui-prefs');
	if (v.perf_view !== undefined) state.perf_view = v.perf_view;
	if (v.show_pads !== undefined) state.show_pads = v.show_pads;
	if (v.auto_play_two_deck !== undefined) state.auto_play_two_deck = v.auto_play_two_deck;
}

/**
 * Build the setters against the live prefs state plus the caller's persist
 * and disk-sync hooks, so every change survives a reload and reaches
 * `PUT /api/v1/ui-prefs` (the HTTP parity path for these chrome prefs).
 */
export function makeTopbarViewSetters(
	state: TopbarViewPrefsState,
	persist: () => void,
	syncDiskPrefs: (patch: Partial<TopbarViewPrefsState>) => void
): TopbarViewSetters {
	return {
		setPerfView(next: PerfView): void {
			assertPerfView(next);
			state.perf_view = next;
			persist();
			syncDiskPrefs({ perf_view: next });
		},
		setShowPads(next: boolean): void {
			state.show_pads = next;
			persist();
			syncDiskPrefs({ show_pads: next });
		},
		setAutoPlayTwoDeck(next: boolean): void {
			state.auto_play_two_deck = next;
			persist();
			syncDiskPrefs({ auto_play_two_deck: next });
		}
	};
}
