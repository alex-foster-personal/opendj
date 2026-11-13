/**
 * Deck grid adjust / grid shift: the pure half (GRIDEDIT-6235).
 *
 * the maintainer, Wed 7 Oct 2026: "have an agent build these for later. (Top and Bottom
 * grid shifters)". The two left-edge deck buttons mirror rekordbox's deck
 * grid-edit controls:
 *
 * - grid adjust (top) moves every beat later or earlier in time, tempo
 *   unchanged, so downbeats line up with the kick;
 * - grid shift (bottom) moves bar 1 by whole beats, so bar 1 lands on the
 *   phrase start you hear.
 *
 * The edit is stored server side (apps/shared/state/grid_edit.py) and applied
 * when `/anlz` serves the grid, so this module only decides WHAT a press asks
 * for and how the edit in force reads in a title. No DOM, no fetch.
 *
 * Step sizes, and why:
 * - 2 ms per grid-adjust click. A kick's attack is a few ms long and two grid
 *   lines a flam apart read as wrong at about 5 to 10 ms, so a 2 ms step lands
 *   inside that window from either side in a few clicks; 1 ms would take twice
 *   the clicks for a difference under a pixel of the strip waveform.
 * - 10 ms with Shift, for a grid that is visibly off (a 30 ms miss is three
 *   clicks, not fifteen).
 * - Hold-to-repeat after 350 ms, every 80 ms (25 ms of travel per second at
 *   the fine step), the same feel as a held nudge key.
 * - Grid shift is one whole beat per click and never repeats on hold: four
 *   beats is the same bar 1, so a repeat would only overshoot.
 */

import type { AnlzBeatgridEdit, AnlzBeatgridSource, AnlzData } from './deck-state-types';

/** Re-exported so the grid-edit queue and API modules take it from here. */
export type { AnlzBeatgridSource, AnlzData };
// The IPC half (validation, the press shape) lives in grid-edit-command so the
// performance dispatcher can load it without this module's title text.
import { GRID_ADJUST_MAX_OFFSET_MS, roundMs, type GridEditIntent, type GridEditPart } from './grid-edit-command';
export {
	mergeNudges,
	type GridEditIntent,
	type GridEditPart
} from './grid-edit-command';

/** Grid-adjust step per click, in ms. */
export const GRID_ADJUST_STEP_MS = 2;
/** Grid-adjust step with Shift held, in ms. */
export const GRID_ADJUST_COARSE_STEP_MS = 10;
/** Hold-to-repeat: first repeat after this long, in ms. */
export const GRID_ADJUST_REPEAT_DELAY_MS = 350;
/** Hold-to-repeat: then one step every this many ms. */
export const GRID_ADJUST_REPEAT_INTERVAL_MS = 80;

interface _RepeatTimers {
	setTimeout(fn: () => void, ms: number): unknown;
	setInterval(fn: () => void, ms: number): unknown;
	clearTimeout(id: unknown): void;
	clearInterval(id: unknown): void;
}

const _GLOBAL_TIMERS: _RepeatTimers = {
	setTimeout: (fn, ms) => setTimeout(fn, ms),
	setInterval: (fn, ms) => setInterval(fn, ms),
	clearTimeout: (id) => clearTimeout(id as ReturnType<typeof setTimeout>),
	clearInterval: (id) => clearInterval(id as ReturnType<typeof setInterval>)
};

/** Hold-to-repeat: `fire` once `delayMs` after the press, then every
 * `intervalMs`, until the returned stop is called. The first repeat lands at
 * `delayMs`, not one interval after it. */
export function startHoldRepeat(
	fire: () => void,
	delayMs: number,
	intervalMs: number,
	timers: _RepeatTimers = _GLOBAL_TIMERS
): () => void {
	let interval: unknown = null;
	let stopped = false;
	const delay = timers.setTimeout(() => {
		fire();
		// The first fire can stop the hold (startHeldGridNudge on a target
		// change); no interval then (Codex P3 and Sentry on PR #8127).
		if (!stopped) interval = timers.setInterval(fire, intervalMs);
	}, delayMs);
	return () => {
		stopped = true;
		timers.clearTimeout(delay);
		if (interval !== null) timers.clearInterval(interval);
	};
}

/** What a held grid-adjust press edits: the loaded track, which load of it,
 * and which grid. */
export interface GridHoldTarget {
	stable_id: string | null;
	load_generation: number;
	source: AnlzBeatgridSource | null;
}

export function gridHoldTarget(deck: {
	stable_id: string | null;
	load_generation: number;
	anlz: AnlzData | null;
}): GridHoldTarget {
	return { stable_id: deck.stable_id, load_generation: deck.load_generation, source: deck.anlz?.beatgrid.source ?? null };
}

/** Codex P2 on PR #6235: a held grid adjust stops repeating when the deck
 * loads another track or switches grid, since each repeat is a new command
 * bound to whatever the deck holds then. The first press already ran. */
export function startHeldGridNudge(
	fire: () => void,
	pressed: GridHoldTarget,
	current: () => GridHoldTarget,
	delayMs: number,
	intervalMs: number,
	timers: _RepeatTimers = _GLOBAL_TIMERS
): () => void {
	let stopped = false;
	const stopTimers = startHoldRepeat(
		() => {
			if (stopped) return;
			const now = current();
			if (
				now.stable_id !== pressed.stable_id ||
				now.load_generation !== pressed.load_generation ||
				now.source !== pressed.source
			) {
				stop();
				return;
			}
			fire();
		},
		delayMs,
		intervalMs,
		timers
	);
	function stop(): void {
		stopped = true;
		stopTimers();
	}
	return stop;
}

/** +1 moves the grid later, -1 earlier. */
export type GridEditDirection = 1 | -1;


export interface GridEditPress {
	/** Pointer x relative to the button's left edge, or null for a keyboard press. */
	offsetX: number | null;
	/** The button's width in px. */
	width: number;
	shiftKey: boolean;
	altKey: boolean;
}

/**
 * Where a press landed, or null for a keyboard press. A `pointerdown` always has a
 * position but its `detail` is 0 (only `click` counts presses), so `detail === 0`
 * marks a keyboard activation on `click` alone.
 */
export function pressOffsetX(
	type: string,
	detail: number,
	clientX: number,
	left: number
): number | null {
	if (type !== 'pointerdown' && detail === 0) return null;
	return clientX - left;
}

/** Left half earlier, right half later; a keyboard press (no position) is later. */
export function directionFromPress(offsetX: number | null, width: number): GridEditDirection {
	if (offsetX === null || !(width > 0)) return 1;
	return offsetX < width / 2 ? -1 : 1;
}

/** The engine request for one press of `part`. */
export function intentForPress(part: GridEditPart, press: GridEditPress): GridEditIntent {
	if (press.altKey) return { kind: 'reset', part };
	const direction = directionFromPress(press.offsetX, press.width);
	if (part === 'shift') return { kind: 'nudge', offset_ms: 0, beats: direction };
	const step = press.shiftKey ? GRID_ADJUST_COARSE_STEP_MS : GRID_ADJUST_STEP_MS;
	return { kind: 'nudge', offset_ms: direction * step, beats: 0 };
}

/** Cmd on Apple platforms, Ctrl elsewhere (same platform test as
 * `assistantChordUsesMeta` in hotkeys-registry). */
export function primaryModifierHeld(
	event: { metaKey: boolean; ctrlKey: boolean },
	platform: string
): boolean {
	return /mac|iphone|ipad/i.test(platform)
		? event.metaKey && !event.ctrlKey
		: event.ctrlKey && !event.metaKey;
}

/** Keys on a focused button: Left earlier, Right later, Cmd/Ctrl+Z undo
 * (GRIDEDIT-10215); null for any other key. Cmd+Shift+Z stays free for a redo
 * and Cmd+Arrow for the bar jump. */
export function intentForKey(
	part: GridEditPart,
	key: string,
	shiftKey: boolean,
	primaryModifier = false
): GridEditIntent | null {
	if (primaryModifier) return !shiftKey && key.toLowerCase() === 'z' ? { kind: 'undo' } : null;
	const direction: GridEditDirection | null =
		key === 'ArrowLeft' ? -1 : key === 'ArrowRight' ? 1 : null;
	if (direction === null) return null;
	return intentForPress(part, {
		offsetX: direction === 1 ? 1 : 0,
		width: 1,
		shiftKey,
		altKey: false
	});
}

/** Sums two nudges; the server applies the sum exactly as the steps in turn. */
/** "+6 ms", "-2.5 ms", "0 ms". */
export function formatOffsetMs(ms: number): string {
	const rounded = roundMs(ms);
	if (rounded === 0) return '0 ms';
	return `${rounded > 0 ? '+' : '-'}${Math.abs(rounded)} ms`;
}

/** "measured bar 1", "bar 1 one beat later", "bar 1 two beats later", "bar 1 one beat earlier". */
export function formatBeatShift(shift: number): string {
	if (shift === 0) return 'measured bar 1';
	const words = ['zero', 'one', 'two', 'three'];
	const count = Math.abs(shift);
	const name = words[count] ?? String(count);
	return `bar 1 ${name} beat${count === 1 ? '' : 's'} ${shift > 0 ? 'later' : 'earlier'}`;
}

/** GRIDEDIT-9763: why the deck grid buttons are disabled on a locked grid (the server's 409 GRID_LOCKED). */
export const GRID_LOCKED_REASON = 'the beatgrid is locked (GRID_LOCKED) - unlock it to edit the grid';

/** Why a deck's grid cannot be edited, or null when it can. */
export function gridEditUnavailableReason(
	stableId: string | null,
	anlz: AnlzData | null,
	isStickTrack: boolean
): string | null {
	if (stableId === null) return 'no track is loaded on this deck';
	if (isStickTrack) return 'this track plays from a USB stick, and grid edits are stored only for library tracks';
	if (anlz === null) return 'the beatgrid has not loaded yet';
	if (anlz.beatgrid.beats.length === 0) return 'this track has no beatgrid to move';
	// The fallback grid never applies a stored edit, and the refresh after one
	// would drop it for the empty /anlz grid (Codex P2, PR #6235).
	if (anlz.beatgrid.fallback === true) return 'this grid is a provisional estimate until the track is analyzed, and grid edits apply to analyzed grids only';
	if (anlz.beatgrid.locked === true) return GRID_LOCKED_REASON;
	return null;
}

/** The edit in force on `anlz`'s grid; absent means none. */
export function currentGridEdit(anlz: AnlzData | null): AnlzBeatgridEdit | null {
	return anlz?.beatgrid.edit ?? null;
}

function _sourceName(source: AnlzBeatgridSource): string {
	return source === 'own' ? 'own analysis' : 'rekordbox';
}

const _UNDO_HINT = 'Cmd+Z (Ctrl+Z off Mac) undoes the last grid edit when focused.';
const _ADJUST_WHAT =
	'Grid adjust - nudges the beatgrid later or earlier on this deck so downbeats line up with the kick, without changing tempo.';
const _SHIFT_WHAT =
	'Grid shift - slides the whole beatgrid by whole beats so bar 1 lands on the phrase start you hear.';

/** Hover title for grid adjust: what it does, how to drive it, and the edit now in force. */
export function gridAdjustTitle(anlz: AnlzData | null, unavailable: string | null): string {
	if (unavailable !== null) return `${_ADJUST_WHAT} Unavailable: ${unavailable}.`;
	const source = anlz?.beatgrid.source ?? 'rekordbox';
	const offset = currentGridEdit(anlz)?.offset_ms ?? 0;
	return (
		`${_ADJUST_WHAT} Click the right half to move it ${GRID_ADJUST_STEP_MS} ms later, the left half ` +
		`${GRID_ADJUST_STEP_MS} ms earlier; Shift for ${GRID_ADJUST_COARSE_STEP_MS} ms steps, hold to repeat, ` +
		`Alt-click to reset. Arrow keys work when focused. ${_UNDO_HINT} Now: ${formatOffsetMs(offset)} on the ` +
		`${_sourceName(source)} grid (limit ${GRID_ADJUST_MAX_OFFSET_MS} ms either way).`
	);
}

/** Hover title for grid shift: what it does, how to drive it, and the edit now in force. */
export function gridShiftTitle(anlz: AnlzData | null, unavailable: string | null): string {
	if (unavailable !== null) return `${_SHIFT_WHAT} Unavailable: ${unavailable}.`;
	const source = anlz?.beatgrid.source ?? 'rekordbox';
	const shift = currentGridEdit(anlz)?.beat_shift ?? 0;
	return (
		`${_SHIFT_WHAT} Click the right half to move bar 1 one beat later, the left half one beat ` +
		`earlier; Alt-click to reset. Arrow keys work when focused. ${_UNDO_HINT} Now: ${formatBeatShift(shift)} on ` +
		`the ${_sourceName(source)} grid.`
	);
}

/** Lit while the part's edit is not zero. */
export function gridEditPartLit(anlz: AnlzData | null, part: GridEditPart): boolean {
	const edit = currentGridEdit(anlz);
	if (edit === null) return false;
	return part === 'adjust' ? edit.offset_ms !== 0 : edit.beat_shift !== 0;
}
