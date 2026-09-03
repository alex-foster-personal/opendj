/**
 * Loop interval grid view state, one entry per deck.
 *
 * The loop cluster shows either the single big beat-length readout (default)
 * or an opt-in 4-choice interval grid. Which mode is showing, and which window
 * of four lengths the grid offers, are view state rather than engine state -
 * but every UI control still needs a matching typed command, so the state
 * lives here where `performance-ipc` can drive it and `LoopCluster.svelte`
 * only reads it. Picking a length from the grid is NOT view state: it engages
 * a real loop through the existing `beat_loop` command.
 *
 * Bounds live here too so the component, the dispatcher and the tests cannot
 * drift apart on what a legal window is.
 */
/**
 * The deck id, declared inline rather than imported.
 *
 * The same call `presentation-clock-report.ts` and `perf-event-log.ts` make,
 * for the same reason: `deck-slots.ts` is the tree's fan-in ceiling and the
 * quality ratchet holds that key at its measured floor with ZERO headroom on
 * purpose, so the next importer reds the gate for every lane at once. A
 * four-member union is not worth doing that to whoever rebases next.
 */
type DeckId = 1 | 2 | 3 | 4;

/** Loop length bounds, shared with the cluster's halve/double controls. */
export const LOOP_MIN_BEATS = 1;
export const LOOP_MAX_BEATS = 512;

/** The grid offers base, 2x, 4x, 8x, so a legal base has to leave room for
 * its own 8x inside LOOP_MAX_BEATS: 512 is reachable, never exceeded. */
export const LOOP_GRID_CHOICES = 4;
export const LOOP_MAX_GRID_BASE = LOOP_MAX_BEATS / 2 ** (LOOP_GRID_CHOICES - 1);

/** Readout mode and a 4/8/16/32 window are the defaults, so existing muscle
 * memory is untouched until a DJ opts in. */
export const LOOP_DEFAULT_GRID_BASE = 4;

export interface LoopIntervalDeckView {
	/** true = 4-choice interval grid, false = single beat-length readout. */
	gridMode: boolean;
	/** Smallest of the four offered lengths, in beats. */
	gridBase: number;
}

function _defaultView(): LoopIntervalDeckView {
	return { gridMode: false, gridBase: LOOP_DEFAULT_GRID_BASE };
}

export const loopIntervalView = $state<Record<DeckId, LoopIntervalDeckView>>({
	1: _defaultView(),
	2: _defaultView(),
	3: _defaultView(),
	4: _defaultView()
});

// --------------------------------------------------------------- validation

/**
 * A window base that is not a power of two inside the bounds is a caller bug.
 * Reject it rather than rounding it into range, so a malformed IPC payload
 * fails loudly instead of silently retargeting the DJ's loop lengths.
 */
export function assertLoopGridBase(base: number): void {
	if (!Number.isInteger(base) || base < LOOP_MIN_BEATS || base > LOOP_MAX_GRID_BASE) {
		throw new RangeError(
			`loop interval base must be an integer in ${LOOP_MIN_BEATS}..${LOOP_MAX_GRID_BASE}; got ${base}`
		);
	}
	if ((base & (base - 1)) !== 0) {
		throw new RangeError(`loop interval base must be a power of two; got ${base}`);
	}
}

// ------------------------------------------------------------ window math

/** The four lengths offered for a window base, smallest first. */
export function loopIntervalChoices(base: number): number[] {
	assertLoopGridBase(base);
	return Array.from({ length: LOOP_GRID_CHOICES }, (_, i) => base * 2 ** i);
}

/**
 * The window base one power of two down (-1) or up (+1), clamped to the
 * bounds. Returns the base unchanged at either end, which is what lets the
 * cluster disable the control with an honest tooltip instead of offering a
 * shift that does nothing.
 */
export function shiftedLoopIntervalBase(base: number, direction: -1 | 1): number {
	assertLoopGridBase(base);
	return direction < 0
		? Math.max(LOOP_MIN_BEATS, base / 2)
		: Math.min(LOOP_MAX_GRID_BASE, base * 2);
}

// ---------------------------------------------------------------- setters

/** Show the interval grid (true) or the single beat-length readout (false). */
export function setLoopIntervalMode(deck: DeckId, enabled: boolean): void {
	loopIntervalView[deck].gridMode = enabled;
}

/** Move the window of four offered lengths to a new base. */
export function setLoopIntervalBase(deck: DeckId, base: number): void {
	assertLoopGridBase(base);
	loopIntervalView[deck].gridBase = base;
}
