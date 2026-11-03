import { isTextEntryTarget } from '$lib/keyboard/text-entry-target';
import { resolveCommentPinHotkey } from './comment-pin-hotkey-routing';
import { isNativeInteractiveTarget } from './performance-hotkeys-target';

export type PerformanceShortcutAction =
	| { kind: 'space'; quantize: boolean }
	| { kind: 'm' }
	| { kind: 'loop-resize'; factor: 0.5 | 2 }
	| { kind: 'loop-exit' }
	| { kind: 'wave-zoom'; direction: 'in' | 'out' }
	| { kind: 'grid-jump'; bars: number; repeat: boolean };

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
}

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
	if (context.gridJump === true) {
		const bars = gridJumpBars(e, context.deckFocus === true);
		if (bars !== null) return { kind: 'grid-jump', bars, repeat: e.repeat === true };
	}
	if (isTextEntryTarget(e.target) || e.metaKey || e.ctrlKey || e.altKey) return null;
	// Tab always traverses focus, including from body and inert page chrome.
	if (e.key === 'Tab') return null;
	if (e.key === '+' || e.key === '=') return { kind: 'loop-resize', factor: 2 };
	if (e.key === '-' || e.key === '_') return { kind: 'loop-resize', factor: 0.5 };
	if (e.key === ')') return { kind: 'loop-exit' };
	// WAVEZOOM-6802b: - and = already halve and double the loop, so zoom takes
	// the bracket pair: [ shows more of the track, ] shows less.
	if (e.key === '[') return { kind: 'wave-zoom', direction: 'out' };
	if (e.key === ']') return { kind: 'wave-zoom', direction: 'in' };
	return null;
}

/**
 * DECKUX-6794: Cmd/Ctrl+Left and Right (A and D mirror them, the app-wide WASD
 * rule) jump the selected deck on the fixed bar grid, Shift for 8 bars.
 * Text fields keep their own line keys. Cmd+A stays the library's Select all
 * unless the last press was on a deck or mixer channel. Alt is reserved for
 * shadow decks, so an Alt chord is left alone.
 */
function gridJumpBars(e: PerformanceShortcutKeyEvent, deckFocus: boolean): number | null {
	if (!(e.metaKey || e.ctrlKey) || e.altKey) return null;
	if (isTextEntryTarget(e.target)) return null;
	const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
	let direction: 1 | -1;
	if (key === 'ArrowRight' || key === 'd') direction = 1;
	else if (key === 'ArrowLeft' || (key === 'a' && deckFocus)) direction = -1;
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
	}
	return true;
}
