// Shared keyboard shortcuts for /performance. Keep KISS: one module, window
// keydown, ignore when typing in inputs. Loop targets track last interaction
// (engage/resize/hover >250ms on a deck's loop cluster). Space toggles play
// on the most recent channel (load / play / loop target).
import { DECK_IDS, getDeckState } from '$lib/rb/audio-engine.svelte';
import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';
import { getRecentDeck, noteRecentDeck } from '$lib/rb/recent-deck';
import type { DeckId } from '$lib/rb/types';

const HOVER_ARM_MS = 250;
const MIN_BEATS = 1;
const MAX_BEATS = 512;

let lastLoopDeck: DeckId | null = null;
let hoverTimer: ReturnType<typeof setTimeout> | null = null;
let hoverDeck: DeckId | null = null;

export function noteLoopInteraction(deck: DeckId): void {
	lastLoopDeck = deck;
	noteRecentDeck(deck);
}

export function armLoopHover(deck: DeckId): void {
	hoverDeck = deck;
	if (hoverTimer !== null) clearTimeout(hoverTimer);
	hoverTimer = setTimeout(() => {
		if (hoverDeck === deck) {
			lastLoopDeck = deck;
			noteRecentDeck(deck);
		}
		hoverTimer = null;
	}, HOVER_ARM_MS);
}

export function clearLoopHover(deck: DeckId): void {
	if (hoverDeck === deck) hoverDeck = null;
	if (hoverTimer !== null) {
		clearTimeout(hoverTimer);
		hoverTimer = null;
	}
}

function _typingTarget(t: EventTarget | null): boolean {
	if (!(t instanceof HTMLElement)) return false;
	const tag = t.tagName;
	return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || t.isContentEditable;
}

function _resolveTransportDeck(): DeckId | null {
	const recent = getRecentDeck();
	if (recent !== null && getDeckState(recent).stable_id !== null) return recent;
	for (const d of DECK_IDS) {
		if (getDeckState(d).playing && getDeckState(d).stable_id !== null) {
			noteRecentDeck(d);
			return d;
		}
	}
	for (const d of DECK_IDS) {
		if (getDeckState(d).stable_id !== null) {
			noteRecentDeck(d);
			return d;
		}
	}
	return null;
}

async function _toggleRecentPlay(): Promise<void> {
	const deck = _resolveTransportDeck();
	if (deck === null) return;
	const st = getDeckState(deck);
	if (st.stable_id === null) return;
	noteRecentDeck(deck);
	await runPerformanceCommandFromUi({ type: 'play', deck, playing: !st.playing });
}

async function _resizeLast(factor: 0.5 | 2): Promise<void> {
	const deck = lastLoopDeck;
	if (deck === null) return;
	const st = getDeckState(deck);
	if (st.loop === null || !st.loop.engaged || st.loop.beat_length === null) return;
	const next =
		factor === 0.5
			? Math.max(MIN_BEATS, Math.floor(st.loop.beat_length / 2))
			: Math.min(MAX_BEATS, st.loop.beat_length * 2);
	if (next === st.loop.beat_length) return;
	await runPerformanceCommandFromUi({
		type: 'beat_loop',
		deck,
		beats: next,
		start_ms: st.loop.in_ms
	});
}

async function _exitLast(): Promise<void> {
	const deck = lastLoopDeck;
	if (deck === null) return;
	const st = getDeckState(deck);
	if (st.loop === null) return;
	await runPerformanceCommandFromUi({ type: 'loop', deck, loop: null });
}

export function installPerformanceHotkeys(): () => void {
	const onKey = (e: KeyboardEvent): void => {
		if (_typingTarget(e.target) || e.metaKey || e.ctrlKey || e.altKey) return;
		if (e.code === 'Space' || e.key === ' ') {
			e.preventDefault();
			void _toggleRecentPlay();
		} else if (e.key === '+' || e.key === '=') {
			e.preventDefault();
			void _resizeLast(2);
		} else if (e.key === '-' || e.key === '_') {
			e.preventDefault();
			void _resizeLast(0.5);
		} else if (e.key === ')') {
			e.preventDefault();
			void _exitLast();
		}
	};
	window.addEventListener('keydown', onKey);
	return () => window.removeEventListener('keydown', onKey);
}

/** Fallback: if no interaction yet, prefer any engaged loop (lowest deck id). */
export function ensureLastLoopDeck(): DeckId | null {
	if (lastLoopDeck !== null && getDeckState(lastLoopDeck).loop !== null) return lastLoopDeck;
	for (const d of DECK_IDS) {
		if (getDeckState(d).loop !== null) {
			lastLoopDeck = d;
			return d;
		}
	}
	return lastLoopDeck;
}
