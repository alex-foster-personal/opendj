/**
 * Pure visibility state machine for the LIBUX-04 hotkeys overlay.
 *
 * "/" hold and "?" toggle both drive the SAME overlay, but they are
 * independent contributions, not one boolean:
 *
 *   visible = holding OR toggledOn
 *
 * Releasing "/" clears the hold contribution only. Pressing "?" while
 * holding "/" sets toggledOn, so the overlay stays after release. Pressing
 * "?" again while not holding clears toggledOn and hides it. Esc / X always
 * wins: it clears BOTH contributions, plus view and query, so a list-view
 * or filtered state cannot trap the overlay open.
 *
 * Esc does not call keyUp() on the slash gesture. If "/" is still physically
 * down, createHoldOrPress stays `down` so OS key-repeat cannot re-fire
 * onHoldStart and pop the overlay back up. The matching keyup then runs
 * onHoldEnd, which is a no-op on already-cleared holding state.
 *
 * The slash path uses createHoldOrPress with no onPress and threshold 0
 * (hold-only, same shape as LIBUX-05's Opt gesture). "?" is a plain toggle.
 */
import { createHoldOrPress } from '$lib/gestures/hold-or-press';
import type { HotkeysOverlayView } from './hotkeys-filter';

export interface HotkeysOverlayGestureState {
	holding: boolean;
	toggledOn: boolean;
	view: HotkeysOverlayView;
	query: string;
}

export function initialHotkeysOverlayGestureState(): HotkeysOverlayGestureState {
	return { holding: false, toggledOn: false, view: 'grid', query: '' };
}

export function isHotkeysOverlayVisible(state: HotkeysOverlayGestureState): boolean {
	return state.holding || state.toggledOn;
}

export function applySlashHoldStart(
	state: HotkeysOverlayGestureState
): HotkeysOverlayGestureState {
	return { ...state, holding: true };
}

export function applySlashHoldEnd(state: HotkeysOverlayGestureState): HotkeysOverlayGestureState {
	return { ...state, holding: false };
}

export function applyToggle(state: HotkeysOverlayGestureState): HotkeysOverlayGestureState {
	return { ...state, toggledOn: !state.toggledOn };
}

export function applyShow(state: HotkeysOverlayGestureState): HotkeysOverlayGestureState {
	return { ...state, toggledOn: true };
}

/** Esc / X: both contributions die, view and query reset. */
export function applyHide(state: HotkeysOverlayGestureState): HotkeysOverlayGestureState {
	return { holding: false, toggledOn: false, view: 'grid', query: '' };
}

export function applyView(
	state: HotkeysOverlayGestureState,
	view: HotkeysOverlayView
): HotkeysOverlayGestureState {
	return { ...state, view };
}

export function applyQuery(
	state: HotkeysOverlayGestureState,
	query: string
): HotkeysOverlayGestureState {
	return { ...state, query };
}

export interface HotkeysOverlayMachine {
	slashKeyDown(): void;
	slashKeyUp(): void;
	resolveBlur(): void;
	toggle(): void;
	show(): void;
	hide(): void;
	setView(view: HotkeysOverlayView): void;
	setQuery(query: string): void;
	isVisible(): boolean;
	getState(): HotkeysOverlayGestureState;
	readonly slashDown: boolean;
	readonly slashHolding: boolean;
}

export function createHotkeysOverlayMachine(): HotkeysOverlayMachine {
	let state = initialHotkeysOverlayGestureState();

	const slash = createHoldOrPress({
		holdThresholdMs: 0,
		onHoldStart: () => {
			state = applySlashHoldStart(state);
		},
		onHoldEnd: () => {
			state = applySlashHoldEnd(state);
		}
	});

	return {
		slashKeyDown: () => slash.keyDown(),
		slashKeyUp: () => slash.keyUp(),
		resolveBlur: () => {
			// Threshold is 0, so down always means holding. keyUp() is the
			// resolving path (same as Opt in technically-working-hotkeys.ts).
			if (slash.down) slash.keyUp();
		},
		toggle: () => {
			state = applyToggle(state);
		},
		show: () => {
			state = applyShow(state);
		},
		hide: () => {
			state = applyHide(state);
		},
		setView: (view) => {
			state = applyView(state, view);
		},
		setQuery: (query) => {
			state = applyQuery(state, query);
		},
		isVisible: () => isHotkeysOverlayVisible(state),
		getState: () => ({ ...state }),
		get slashDown() {
			return slash.down;
		},
		get slashHolding() {
			return slash.holding;
		}
	};
}
