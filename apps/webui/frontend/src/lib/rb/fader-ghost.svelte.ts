/**
 * Ghost fader divergence and rejoin guidance (MIXUX-08 AC5).
 */
import type { DeckId } from './deck-slots';

export const FADER_GHOST_EPSILON = 0.01;
export const FADER_IDLE_MS = 2000;
export const FADER_GUIDANCE_CYCLE_MS = 3000;
export const FADER_ARROW_DOTTED_MIN_PX = 24;

export interface FaderGhostDeckState {
	ghostValue: number | null;
	diverged: boolean;
	lastPointerY: number | null;
	lastPointerDirection: -1 | 0 | 1;
	idleSince: number | null;
	showGuidance: boolean;
}

const DECK_IDS: DeckId[] = [1, 2, 3, 4];

function _emptyDeckState(): FaderGhostDeckState {
	return {
		ghostValue: null,
		diverged: false,
		lastPointerY: null,
		lastPointerDirection: 0,
		idleSince: null,
		showGuidance: false
	};
}

function _initial(): Record<DeckId, FaderGhostDeckState> {
	return {
		1: _emptyDeckState(),
		2: _emptyDeckState(),
		3: _emptyDeckState(),
		4: _emptyDeckState()
	};
}

export const faderGhostByDeck = $state<Record<DeckId, FaderGhostDeckState>>(_initial());

export function valuesReconciled(ghost: number, software: number, epsilon = FADER_GHOST_EPSILON): boolean {
	return Math.abs(ghost - software) < epsilon;
}

export function shouldShowGuidanceAfterIdle(
	idleSince: number | null,
	now: number,
	thresholdMs = FADER_IDLE_MS
): boolean {
	if (idleSince === null) return false;
	return now - idleSince >= thresholdMs;
}

export function pointerDirectionTowardSoftware(
	prevY: number,
	nextY: number,
	softwareThumbY: number
): -1 | 0 | 1 {
	const prevDist = Math.abs(prevY - softwareThumbY);
	const nextDist = Math.abs(nextY - softwareThumbY);
	if (nextDist < prevDist - 0.5) {
		if (nextY < prevY) return -1;
		if (nextY > prevY) return 1;
	}
	return 0;
}

export function detectPointerReversal(
	lastDirection: -1 | 0 | 1,
	newDirection: -1 | 0 | 1
): boolean {
	return lastDirection !== 0 && newDirection !== 0 && lastDirection !== newDirection;
}

function _deck(deck: DeckId): FaderGhostDeckState {
	return faderGhostByDeck[deck];
}

function _setDeck(deck: DeckId, next: FaderGhostDeckState): void {
	faderGhostByDeck[deck] = next;
}

export function clearGhost(deck: DeckId): void {
	_setDeck(deck, _emptyDeckState());
}

export function clearGhostIfReconciled(deck: DeckId, software: number): void {
	const state = _deck(deck);
	if (state.ghostValue === null) return;
	if (valuesReconciled(state.ghostValue, software)) {
		clearGhost(deck);
	}
}

export function openGhost(deck: DeckId, anchor: number, software: number): void {
	if (valuesReconciled(anchor, software)) {
		clearGhost(deck);
		return;
	}
	const prev = _deck(deck);
	const now = performance.now();
	_setDeck(deck, {
		...prev,
		ghostValue: anchor,
		diverged: true,
		showGuidance: false,
		idleSince: now
	});
}

export function onWheelFaderAdjust(
	deck: DeckId,
	before: number,
	after: number,
	pointerOnFader: boolean
): void {
	if (pointerOnFader) return;
	openGhost(deck, before, after);
}

export function onMidiFaderMove(deck: DeckId, software: number, lastUiValue: number): void {
	if (!valuesReconciled(lastUiValue, software)) {
		openGhost(deck, lastUiValue, software);
	}
	clearGhostIfReconciled(deck, software);
}

export function onFaderPointerMove(
	deck: DeckId,
	clientY: number,
	softwareValue: number,
	thumbTopPx: number,
	trackHeight: number
): void {
	const state = _deck(deck);
	if (!state.diverged || state.ghostValue === null) return;
	const softwareThumbY = thumbTopPx + 6;
	const direction = pointerDirectionTowardSoftware(state.lastPointerY ?? clientY, clientY, softwareThumbY);
	const reversed = detectPointerReversal(state.lastPointerDirection, direction);
	const moved = state.lastPointerY === null || Math.abs(clientY - state.lastPointerY) > 0.5;
	const idleSince = moved ? performance.now() : (state.idleSince ?? performance.now());
	const showGuidance =
		reversed ||
		shouldShowGuidanceAfterIdle(idleSince, performance.now()) ||
		state.showGuidance;
	_setDeck(deck, {
		...state,
		lastPointerY: clientY,
		lastPointerDirection: direction !== 0 ? direction : state.lastPointerDirection,
		idleSince,
		showGuidance
	});
	clearGhostIfReconciled(deck, softwareValue);
}

export function onFaderPointerLeave(deck: DeckId): void {
	const state = _deck(deck);
	if (!state.diverged) return;
	_setDeck(deck, {
		...state,
		lastPointerY: null
	});
}

export function onFaderDragEnd(
	deck: DeckId,
	pointerValue: number,
	committedValue: number
): void {
	if (!valuesReconciled(pointerValue, committedValue)) {
		openGhost(deck, pointerValue, committedValue);
	}
	clearGhostIfReconciled(deck, committedValue);
}

export function ghostThumbTopPx(deck: DeckId, trackHeight: number, thumbH = 12): number | null {
	const ghost = _deck(deck).ghostValue;
	if (ghost === null) return null;
	return (1 - ghost) * Math.max(1, trackHeight - thumbH);
}

export function guidanceVisible(deck: DeckId): boolean {
	const state = _deck(deck);
	return state.diverged && state.showGuidance && state.ghostValue !== null;
}

export function rejoinGlowActive(deck: DeckId): boolean {
	return guidanceVisible(deck);
}

/** Advance idle guidance for all diverged decks (rAF or pointer tick). */
export function tickFaderGhostIdle(now: number): void {
	for (const deck of DECK_IDS) {
		const state = _deck(deck);
		if (!state.diverged || state.ghostValue === null) continue;
		// Once only: this runs every animation frame, and a write here replaces the
		// deck's $state object, re-running every ghost reader at 60 Hz.
		if (!state.showGuidance && shouldShowGuidanceAfterIdle(state.idleSince, now)) {
			_setDeck(deck, { ...state, showGuidance: true });
		}
	}
}
