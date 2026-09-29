// Shared keyboard shortcuts for /performance. Keep KISS: one module, window
// keydown, ignore when typing in inputs. Loop targets track last interaction
// (engage/resize/hover >250ms on a deck's loop cluster). Space toggles play
// on the most recent channel (load / play / loop target). Tab toggles library
// next-only filter (Camelot + BPM window).
// Cmd+, / Ctrl+, is owned by settings/hotkeys.ts (meta/ctrl ignored here).
import { DECK_IDS, getDeckState } from '$lib/rb/audio-engine.svelte';
import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';
import { getRecentDeck, noteRecentDeck } from '$lib/rb/recent-deck';
import { toggleNextOnlyFilter } from '$lib/rb/prefs.svelte';
import { mostRecentPendingLoadPlay, setPendingLoadPlayIntent, type DeckId } from '$lib/rb/deck-slots';
import { isSettingsOpen } from '$lib/settings/overlay.svelte';
import { armPinPlacement } from './feedback-store.svelte';
import { handlePerformanceShortcutKeydown } from './performance-shortcut-routing';

// Re-exported so noteLoopInteraction's callers (e.g. LoopSafetyControls.svelte)
// can take DeckId from here instead of a fresh direct import of deck-slots.ts,
// which sits at its frontend.max_fan_in allowance (same pairing as DECK_IDS
// alongside DeckId in $lib/player/constants).
export type { DeckId };

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

/**
 * Q1: `pressT0Ms` is the keydown's own `event.timeStamp`, on the
 * `performance.now()` epoch, threaded from the listener instead of re-read
 * here.
 *
 * Space is the most latency-sensitive press in the app - it is how a DJ starts
 * a track without looking at the screen - and it is also the path with the most
 * to hide before the stamp arrives: the browser queues the key, dispatches to
 * this listener, and only then does anything measurable begin. A
 * `performance.now()` taken downstream starts the clock after all of that and
 * reports a number smaller than the wait the operator actually had.
 *
 * An EMPTY deck returns before dispatching anything, which is what keeps the
 * instrument honest: no command means no schedule, so no press row, so silence
 * can never be quoted as a latency.
 */
async function _toggleRecentPlay(pressT0Ms?: number, quantize?: boolean): Promise<void> {
	const pending = mostRecentPendingLoadPlay();
	if (pending !== null) {
		const desiredPlay = !pending.desiredPlay;
		await runPerformanceCommandFromUi(
			{
				type: 'load_play_intent',
				deck: pending.deck,
				generation: pending.generation,
				desired_play: desiredPlay
			},
			pressT0Ms
		);
		if (quantize === true && desiredPlay) {
			setPendingLoadPlayIntent(pending.deck, pending.generation, desiredPlay, pressT0Ms, true);
		}
		return;
	}
	const deck = _resolveTransportDeck();
	if (deck === null) return;
	const st = getDeckState(deck);
	if (st.stable_id === null) return;
	noteRecentDeck(deck);
	const playing = !st.playing;
	await runPerformanceCommandFromUi(
		{
			type: 'play',
			deck,
			playing,
			...(quantize === true && playing ? { quantize: true } : {})
		},
		pressT0Ms
	);
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
		handlePerformanceShortcutKeydown(
			e,
			{
				toggleRecentPlay: (pressT0Ms, quantize) => {
					void _toggleRecentPlay(pressT0Ms, quantize || undefined);
				},
				toggleNextOnlyFilter,
				resizeLast: (factor) => {
					void _resizeLast(factor);
				},
				exitLast: () => {
					void _exitLast();
				},
				armPinPlacement
			},
			{ settingsOpen: isSettingsOpen() }
		);
	};
	window.addEventListener('keydown', onKey, { capture: true });
	return () => window.removeEventListener('keydown', onKey, { capture: true });
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
