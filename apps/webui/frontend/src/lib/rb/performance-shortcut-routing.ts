import { isTextEntryTarget } from '$lib/keyboard/text-entry-target';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';
import { resolveCommentPinHotkey } from './comment-pin-hotkey-routing';
import { isLibraryTrackListTarget } from './library-track-list-target';
import { isNativeInteractiveTarget } from './performance-hotkeys-target';
import type { DeckId } from '$lib/player/constants';
import type { TempoKeyAction } from './tempo-keys';

/** KEYB-11383: the deck verbs C, V and K send for the recent deck. */
export type RecentDeckVerb = 'cue' | 'beat-sync' | 'master-tempo';

export type PerformanceShortcutAction =
	| { kind: 'space'; quantize: boolean }
	| { kind: 'm' }
	| { kind: 'loop-resize'; factor: 0.5 | 2 }
	| { kind: 'loop-exit' }
	| { kind: 'wave-zoom'; direction: 'in' | 'out' }
	| { kind: 'grid-jump'; bars: number; repeat: boolean }
	| { kind: 'recent-deck'; verb: RecentDeckVerb; repeat: boolean }
	| { kind: 'cycle-deck'; direction: 1 | -1; repeat: boolean }
	| { kind: 'tempo-step'; direction: 1 | -1; coarse: boolean }
	| { kind: 'tempo-reset'; repeat: boolean }
	| { kind: 'pitch-range-cycle'; repeat: boolean }
	| { kind: 'hot-cue-trigger'; slot: HotCueSlot; repeat: boolean }
	| { kind: 'hot-cue-save'; slot: HotCueSlot; repeat: boolean }
	| { kind: 'hot-cue-clear'; slot: HotCueSlot; repeat: boolean };

export interface PerformanceShortcutKeyEvent {
	code: string;
	key: string;
	target: EventTarget | null;
	metaKey: boolean;
	ctrlKey: boolean;
	altKey: boolean;
	shiftKey?: boolean;
	repeat?: boolean;
}

/** Context the pure router cannot read from the event itself. */
export interface PerformanceShortcutContext {
	/** DECKUX-6794: the `deck.snapped_jump` flag is on. */
	gridJump?: boolean;
	/** The last pointer press landed on a deck or mixer channel, so Cmd+A
	 * means "jump back" there instead of the library's Select all. */
	deckFocus?: boolean;
	/** The key's target is inside the library root; Cmd/Ctrl+Shift+arrow there
	 * is the library's instant double (KEYB-11643), so the 8-bar grid jump yields. */
	libraryFocus?: boolean;
	/** #9453: a dial or fader selection owns the digit row. */
	dialSelected?: boolean;
}

export interface PerformanceShortcutKeydownEvent extends PerformanceShortcutKeyEvent {
	preventDefault: () => void;
	stopPropagation?: () => void;
	timeStamp: number;
}

export interface PerformanceShortcutHandlers {
	toggleRecentPlay: (pressT0Ms: number, quantize: boolean) => void;
	resizeLast: (factor: 0.5 | 2) => void;
	exitLast: () => void;
	armPinPlacement: () => void;
	/** WAVEZOOM-6802b: one main-waveform zoom step. */
	waveZoomStep: (direction: 'in' | 'out') => void;
	gridJump?: (bars: number) => void;
	/** KEYB-11383: C cue, V beat sync, K master tempo on the recent deck. */
	recentDeckCommand?: (verb: RecentDeckVerb) => void;
	/** KEYB-11383: Backquote moves the recent deck to the next loaded deck. */
	cycleRecentDeck?: (direction: 1 | -1) => void;
	/** #10357: tempo step, reset and pitch-range cycle on the recent deck. */
	tempoKey?: (action: TempoKeyAction) => void;
	hotCueTrigger?: (slot: HotCueSlot) => void;
	hotCueSave?: (slot: HotCueSlot) => void;
	hotCueClear?: (slot: HotCueSlot) => void;
}

const RECENT_DECK_LETTERS: Readonly<Record<string, RecentDeckVerb>> = {
	c: 'cue',
	v: 'beat-sync',
	k: 'master-tempo'
};

/** Pure routing for /performance global shortcuts; null means leave the event alone. */
export function resolvePerformanceShortcutAction(
	e: PerformanceShortcutKeyEvent,
	context: PerformanceShortcutContext = {}
): PerformanceShortcutAction | null {
	if (e.code === 'Space' || e.key === ' ') {
		// Focused controls own Space activation; page chrome keeps transport.
		if (isTextEntryTarget(e.target)) return null;
		if (isNativeInteractiveTarget(e.target)) return null;
		if (e.metaKey || e.ctrlKey) {
			if (e.altKey) return null;
			return { kind: 'space', quantize: true };
		}
		if (e.altKey) return null;
		return { kind: 'space', quantize: false };
	}
	if (e.key === 'm' || e.key === 'M') {
		return resolveCommentPinHotkey({
			key: e.key,
			target: e.target,
			metaKey: e.metaKey,
			ctrlKey: e.ctrlKey,
			altKey: e.altKey,
			shiftKey: e.shiftKey ?? false
		}) === 'arm'
			? { kind: 'm' }
			: null;
	}
	const hotCue = hotCueDigitAction(e, context.dialSelected === true);
	if (hotCue !== null) return hotCue;
	if (context.gridJump === true) {
		const bars = gridJumpBars(e, context.deckFocus === true, context.libraryFocus === true);
		if (bars !== null) return { kind: 'grid-jump', bars, repeat: e.repeat === true };
	}
	if (isTextEntryTarget(e.target) || e.metaKey || e.ctrlKey || e.altKey) return null;
	// Tab always traverses focus, including from body and inert page chrome.
	if (e.key === 'Tab') return null;
	// KEYB-11383: Backquote by code (Shift turns the key into ~), C/V/K by letter.
	if (e.code === 'Backquote')
		return {
			kind: 'cycle-deck',
			direction: e.shiftKey === true ? -1 : 1,
			repeat: e.repeat === true
		};
	const verb =
		e.shiftKey === true || e.key.length !== 1
			? undefined
			: RECENT_DECK_LETTERS[e.key.toLowerCase()];
	if (verb !== undefined) return { kind: 'recent-deck', verb, repeat: e.repeat === true };
	if (e.key === '+' || e.key === '=') return { kind: 'loop-resize', factor: 2 };
	if (e.key === '-' || e.key === '_') return { kind: 'loop-resize', factor: 0.5 };
	if (e.key === ')') return { kind: 'loop-exit' };
	// WAVEZOOM-6802b: - and = already halve and double the loop, so zoom takes
	// the bracket pair: [ shows more of the track, ] shows less.
	if (e.key === '[') return { kind: 'wave-zoom', direction: 'out' };
	if (e.key === ']') return { kind: 'wave-zoom', direction: 'in' };
	if (e.key === '.' || e.key === '>') {
		return { kind: 'tempo-step', direction: 1, coarse: e.shiftKey === true };
	}
	if (e.key === ',' || e.key === '<') {
		return { kind: 'tempo-step', direction: -1, coarse: e.shiftKey === true };
	}
	// Plain k is reserved for master tempo (#10352).
	if (e.shiftKey === true && (e.key === 'K' || e.key === 'k')) {
		return { kind: 'tempo-reset', repeat: e.repeat === true };
	}
	if (e.shiftKey !== true && (e.key === 'p' || e.key === 'P')) {
		return { kind: 'pitch-range-cycle', repeat: e.repeat === true };
	}
	return null;
}

const HOT_CUE_SLOTS = [
	'A',
	'B',
	'C',
	'D',
	'E',
	'F',
	'G',
	'H'
] as const satisfies readonly HotCueSlot[];

/**
 * #10354: digits 1-8 on the transport deck's hot cue pads A-H (rekordbox 1-4,
 * Serato digit cues). Match `event.code` Digit1..Digit8 so Shift+N is still N.
 * Cmd+N without Shift falls through for deck layout / cmdk quick pick.
 */
function hotCueDigitAction(
	e: PerformanceShortcutKeyEvent,
	dialSelected: boolean
): PerformanceShortcutAction | null {
	const m = /^Digit([1-8])$/.exec(e.code);
	if (m === null) return null;
	if (dialSelected || e.altKey || isTextEntryTarget(e.target)) return null;
	const slot = HOT_CUE_SLOTS[Number(m[1]) - 1];
	const repeat = e.repeat === true;
	const mod = e.metaKey || e.ctrlKey;
	const shift = e.shiftKey === true;
	if (!mod && !shift) return { kind: 'hot-cue-trigger', slot, repeat };
	if (!mod && shift) return { kind: 'hot-cue-save', slot, repeat };
	if (mod && shift) return { kind: 'hot-cue-clear', slot, repeat };
	return null;
}

/**
 * DECKUX-6794: Cmd/Ctrl+Left and Right (A and D mirror them, the app-wide WASD
 * rule) jump the selected deck on the fixed bar grid, Shift for 8 bars.
 * Text fields keep their own line keys. Cmd+A stays the library's Select all
 * unless the last press was on a deck or mixer channel. Alt is reserved for
 * shadow decks, so an Alt chord is left alone.
 */
function gridJumpBars(
	e: PerformanceShortcutKeyEvent,
	deckFocus: boolean,
	libraryFocus: boolean
): number | null {
	if (!(e.metaKey || e.ctrlKey) || e.altKey) return null;
	if (isTextEntryTarget(e.target)) return null;
	const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
	if (e.shiftKey === true && libraryFocus && (key === 'ArrowLeft' || key === 'ArrowRight'))
		return null;
	let direction: 1 | -1;
	if (key === 'ArrowRight' || key === 'd') direction = 1;
	else if (key === 'ArrowLeft' || (key === 'a' && deckFocus && !isLibraryTrackListTarget(e.target)))
		direction = -1;
	else return null;
	return direction * (e.shiftKey === true ? 8 : 4);
}

/** Applies a resolved shortcut: preventDefault plus the registered side effect. */
export function handlePerformanceShortcutKeydown(
	e: PerformanceShortcutKeydownEvent,
	handlers: PerformanceShortcutHandlers,
	options: { settingsOpen?: boolean } & PerformanceShortcutContext = {}
): boolean {
	if (options.settingsOpen === true) return false;
	const action = resolvePerformanceShortcutAction(e, options);
	if (action === null) return false;
	e.preventDefault();
	switch (action.kind) {
		case 'space':
			handlers.toggleRecentPlay(e.timeStamp, action.quantize);
			break;
		case 'loop-resize':
			handlers.resizeLast(action.factor);
			break;
		case 'loop-exit':
			handlers.exitLast();
			break;
		case 'm':
			handlers.armPinPlacement();
			break;
		case 'wave-zoom':
			handlers.waveZoomStep(action.direction);
			break;
		case 'grid-jump':
			// One tap is one jump: a held key never runs through the track.
			// The chord is still taken, so Cmd+A/D never fall through to
			// Select all or a browser bookmark mid-hold.
			e.stopPropagation?.();
			if (!action.repeat) handlers.gridJump?.(action.bars);
			break;
		case 'recent-deck':
			if (!action.repeat) handlers.recentDeckCommand?.(action.verb);
			break;
		case 'cycle-deck':
			if (!action.repeat) handlers.cycleRecentDeck?.(action.direction);
			break;
		case 'tempo-step':
			handlers.tempoKey?.({
				kind: 'tempo-step',
				direction: action.direction,
				coarse: action.coarse
			});
			break;
		case 'tempo-reset':
			if (!action.repeat) handlers.tempoKey?.({ kind: 'tempo-reset' });
			break;
		case 'pitch-range-cycle':
			if (!action.repeat) handlers.tempoKey?.({ kind: 'pitch-range-cycle' });
			break;
		case 'hot-cue-trigger':
		case 'hot-cue-save':
		case 'hot-cue-clear':
			e.stopPropagation?.();
			if (!action.repeat) {
				if (action.kind === 'hot-cue-trigger') handlers.hotCueTrigger?.(action.slot);
				else if (action.kind === 'hot-cue-save') handlers.hotCueSave?.(action.slot);
				else handlers.hotCueClear?.(action.slot);
			}
			break;
	}
	return true;
}

export type RecentDeckCommand =
	| { type: 'cue'; deck: DeckId }
	| { type: 'beat_sync'; deck: DeckId; enabled: boolean }
	| { type: 'master_tempo'; deck: DeckId; enabled: boolean };

/** KEYB-11383: the bus command for a recent-deck chord, toggles flipped from
 * the deck's state. An empty deck sends nothing (the Space rule). */
export function buildRecentDeckCommand(
	verb: RecentDeckVerb,
	deck: DeckId | null,
	state: {
		stable_id: string | null;
		beat_sync_enabled: boolean;
		master_tempo_enabled: boolean;
	} | null
): RecentDeckCommand | null {
	if (deck === null || state === null || state.stable_id === null) return null;
	switch (verb) {
		case 'cue':
			return { type: 'cue', deck };
		case 'beat-sync':
			return { type: 'beat_sync', deck, enabled: !state.beat_sync_enabled };
		case 'master-tempo':
			return { type: 'master_tempo', deck, enabled: !state.master_tempo_enabled };
		default: {
			const _exhaustive: never = verb;
			throw new Error(`Unhandled recent-deck verb: ${String(_exhaustive)}`);
		}
	}
}
