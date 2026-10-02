/**
 * MORE/LESS two-deck performance layout preference (pin 862cd3): the type
 * + bounds definitions and the fail-fast validators for its three
 * persisted fields, split out of prefs.svelte.ts.
 *
 * Deliberately NOT a rune module (no $state here) - these are pure
 * type/validation helpers the reactive singleton in prefs.svelte.ts calls
 * into from both its localStorage load path and its setDeckLayoutDurationMs
 * setter, so the bounds check lives in exactly one place.
 */

/**
 * MORE/LESS two-deck performance layout (pin 862cd3). 'more' = all four
 * decks/mixer strips/wave rows visible (default). 'less' collapses deck 3/4
 * chrome only - their transport/DSP state and IPC control are untouched -
 * and gives the library panel the released space.
 */
export type DeckLayoutMode = 'more' | 'less';

/** Allowed transition durations (ms) for the MORE/LESS switch. */
export const DECK_LAYOUT_DURATIONS_MS = [0, 100, 200, 300, 400] as const;
export type DeckLayoutDurationMs = (typeof DECK_LAYOUT_DURATIONS_MS)[number];

/** Fail-fast: an unlisted duration is a bug in the caller, not a silent clamp. */
export function assertValidDeckLayoutDurationMs(next: number): void {
	if (!(DECK_LAYOUT_DURATIONS_MS as readonly number[]).includes(next)) {
		throw new Error(
			`deck layout duration must be one of ${DECK_LAYOUT_DURATIONS_MS.join(', ')}ms, got ${String(next)}`
		);
	}
}

/** Validate the three deck-layout fields of a parsed prefs blob (all optional -
 * absent means "use the caller's default"), throwing with `storageKey` in the
 * message on any malformed value so the thrown text matches whichever
 * caller's storage key backs it. */
export function validateDeckLayoutFields(
	parsed: {
		deck_layout?: unknown;
		deck_layout_animate?: unknown;
		deck_layout_duration_ms?: unknown;
		deck_right_mirror?: unknown;
	},
	storageKey: string
): {
	deck_layout: DeckLayoutMode | undefined;
	deck_layout_animate: boolean | undefined;
	deck_layout_duration_ms: DeckLayoutDurationMs | undefined;
	deck_right_mirror: boolean | undefined;
} {
	const deckLayout = parsed.deck_layout;
	if (deckLayout !== undefined && deckLayout !== 'more' && deckLayout !== 'less') {
		throw new Error(
			`${storageKey}: malformed prefs blob (deck_layout must be 'more'|'less') - ` +
				'clear the localStorage key to recover'
		);
	}
	if (parsed.deck_layout_animate !== undefined && typeof parsed.deck_layout_animate !== 'boolean') {
		throw new Error(
			`${storageKey}: malformed prefs blob (deck_layout_animate is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	const deckLayoutDurationMs = parsed.deck_layout_duration_ms;
	if (
		deckLayoutDurationMs !== undefined &&
		!(DECK_LAYOUT_DURATIONS_MS as readonly number[]).includes(deckLayoutDurationMs as number)
	) {
		throw new Error(
			`${storageKey}: malformed prefs blob (deck_layout_duration_ms must be one of ` +
				`${DECK_LAYOUT_DURATIONS_MS.join(', ')}) - clear the localStorage key to recover`
		);
	}
	if (parsed.deck_right_mirror !== undefined && typeof parsed.deck_right_mirror !== 'boolean') {
		throw new Error(
			`${storageKey}: malformed prefs blob (deck_right_mirror is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	return {
		deck_layout: deckLayout as DeckLayoutMode | undefined,
		deck_layout_animate: parsed.deck_layout_animate as boolean | undefined,
		deck_layout_duration_ms: deckLayoutDurationMs as DeckLayoutDurationMs | undefined,
		deck_right_mirror: parsed.deck_right_mirror as boolean | undefined
	};
}

/** The slice of RbUiPrefs the deck-layout setters below read and write. */
export interface DeckLayoutPrefsState {
	deck_layout: DeckLayoutMode;
	deck_layout_animate: boolean;
	deck_layout_duration_ms: DeckLayoutDurationMs;
	/** Mirror deck 2 main control row for right-column symmetry (issue #3983). */
	deck_right_mirror: boolean;
	/** Mirror deck 1 and 3 control rows. Local-only (not disk-synced). */
	deck_left_mirror: boolean;
}

export interface DeckLayoutSetters {
	setDeckLayoutMode(next: DeckLayoutMode): void;
	toggleDeckLayoutMode(): void;
	setDeckLayoutAnimate(next: boolean): void;
	setDeckLayoutDurationMs(next: DeckLayoutDurationMs): void;
	setDeckRightMirror(next: boolean): void;
	setDeckLeftMirror(next: boolean): void;
}

/**
 * Build the MORE/LESS deck-layout setters (pin 862cd3) against a live prefs
 * state + the caller's persist/disk-sync hooks. Chrome-only: never touches
 * deck audio/transport/IPC, only the persisted preference that Mixer.svelte
 * / WaveformStack.svelte / +page.svelte read to hide chrome.
 *
 * `state` is passed by reference (the caller's reactive $state object) and
 * mutated in place here, same as the setters did inline in prefs.svelte.ts
 * before this split - a Svelte 5 $state proxy stays reactive regardless of
 * which module holds the reference that mutates it.
 */
export function makeDeckLayoutSetters(
	state: DeckLayoutPrefsState,
	persist: () => void,
	syncDiskPrefs: (patch: Partial<DeckLayoutPrefsState>) => void
): DeckLayoutSetters {
	function setDeckLayoutMode(next: DeckLayoutMode): void {
		state.deck_layout = next;
		persist();
		syncDiskPrefs({ deck_layout: next });
	}

	function toggleDeckLayoutMode(): void {
		setDeckLayoutMode(state.deck_layout === 'less' ? 'more' : 'less');
	}

	function setDeckLayoutAnimate(next: boolean): void {
		state.deck_layout_animate = next;
		persist();
		syncDiskPrefs({ deck_layout_animate: next });
	}

	/** Fail-fast: an unlisted duration is a bug in the caller, not a silent clamp. */
	function setDeckLayoutDurationMs(next: DeckLayoutDurationMs): void {
		assertValidDeckLayoutDurationMs(next);
		state.deck_layout_duration_ms = next;
		persist();
		syncDiskPrefs({ deck_layout_duration_ms: next });
	}

	function setDeckRightMirror(next: boolean): void {
		state.deck_right_mirror = next;
		persist();
		syncDiskPrefs({ deck_right_mirror: next });
	}

	function setDeckLeftMirror(next: boolean): void {
		state.deck_left_mirror = next;
		persist();
	}

	return {
		setDeckLayoutMode,
		toggleDeckLayoutMode,
		setDeckLayoutAnimate,
		setDeckLayoutDurationMs,
		setDeckRightMirror,
		setDeckLeftMirror
	};
}
