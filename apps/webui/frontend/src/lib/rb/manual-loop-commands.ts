/**
 * DECKUX-10478 manual LOOP IN / LOOP OUT / RELOOP-EXIT, split from audio-engine.svelte.ts so the engine barrel
 * stays under its #8581 size ratchet. The decisions are pure (player/transport/loops.ts). This module reads deck
 * state and drives the engine only through ManualLoopPort. Never import audio-engine.svelte.ts or
 * performance-ipc.svelte.ts here (frontend.import_cycles slack is 0): performance-ipc passes the port in.
 */
import { effectiveQuantize } from '$lib/player/grid-features';
import {
	loopGridBeats,
	manualLoopInMs,
	manualLoopOutDecision,
	reloopDecision
} from '$lib/player/transport/loops';
import type { DeckId } from '$lib/player/constants';
import type { AnlzBeat } from '$lib/rb/anlz-types';
import type { DeckState, LoopState } from '$lib/rb/deck-state-types';

export interface ManualLoopPort {
	/** Live DeckState proxy (getDeckState); this module writes pending_loop_in_ms on it. */
	deckState(deck: DeckId): DeckState;
	/** Audio-clock playhead while playing, published cursor when paused (deckAudioClockPositionMs). */
	playheadMs(deck: DeckId): number;
	/** engine.setLoop: the ordinary loop path; null exits. */
	setLoop(
		deck: DeckId,
		loop: { in_ms: number; out_ms: number } | null,
		quantize?: boolean
	): Promise<void>;
	/** engine.jumpIntoLoop: engage `loop` with the playhead at its in point, one transaction. */
	jumpIntoLoop(deck: DeckId, loop: LoopState, op?: string): Promise<void>;
}

function loadedDeck(port: ManualLoopPort, deck: DeckId, op: string): DeckState {
	const st = port.deckState(deck);
	if (st.stable_id === null) throw new Error(`${op}: no track loaded on deck ${deck}`);
	return st;
}

/** Same rule as the engine's _quantizeGrid: the PQTZ beats when Q is effectively on, else null. */
function quantizeBeats(st: DeckState): readonly AnlzBeat[] | null {
	const beats = st.anlz?.beatgrid?.beats;
	return effectiveQuantize(st) && beats !== undefined ? beats : null;
}

export function manualLoopIn(port: ManualLoopPort, deck: DeckId): number {
	const st = loadedDeck(port, deck, 'loop_in');
	st.pending_loop_in_ms = manualLoopInMs(
		quantizeBeats(st),
		port.playheadMs(deck),
		loopGridBeats(st.quantize_grid_beats)
	);
	return st.pending_loop_in_ms;
}

export async function manualLoopOut(port: ManualLoopPort, deck: DeckId): Promise<void> {
	const st = loadedDeck(port, deck, 'loop_out');
	const d = manualLoopOutDecision(deck, st.pending_loop_in_ms, port.playheadMs(deck));
	if (d.kind === 'refused') throw new Error(d.reason);
	await port.setLoop(deck, d.loop, true);
	st.pending_loop_in_ms = null;
}

export async function manualReloop(port: ManualLoopPort, deck: DeckId): Promise<void> {
	const st = loadedDeck(port, deck, 'reloop');
	const d = reloopDecision(deck, st.loop, st.last_loop);
	if (d.kind === 'refused') throw new Error(d.reason);
	if (d.kind === 'exit') return port.setLoop(deck, null);
	await port.jumpIntoLoop(
		deck,
		{
			in_ms: d.loop.in_ms,
			out_ms: d.loop.out_ms,
			beat_length: d.loop.beat_length,
			engaged: true
		},
		'reloop'
	);
}
