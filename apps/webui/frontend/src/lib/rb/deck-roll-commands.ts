/**
 * FXROLL-01 deck roll command logic, split from audio-engine.svelte.ts (#8581 size ratchet).
 * Never import audio-engine.svelte.ts or performance-ipc.svelte.ts here.
 */
import type { DeckId } from '$lib/player/constants';
import { exactBeatLoopRangeMs } from '$lib/player/transport/loops';
import {
	assertRollBeats,
	beginRoll,
	pushRoll,
	releaseRoll,
	rollQueryFields,
	type RollState
} from '$lib/player/transport/roll-state';
import type { DeckState, LoopState } from '$lib/rb/deck-state-types';
import type { AnlzBeat } from '$lib/rb/anlz-types';

const _rollStates: Record<DeckId, RollState | null> = { 1: null, 2: null, 3: null, 4: null };

export interface DeckRollPort {
	deckState(deck: DeckId): DeckState;
	requireLoaded(deck: DeckId, op: string): { st: DeckState; rt: unknown };
	phaseGrid(st: DeckState, op: string): readonly AnlzBeat[];
	currentPosSec(deck: DeckId): number;
	scheduleDeckIntoLoop(deck: DeckId, rt: unknown, loop: LoopState): Promise<void>;
	clearSlip(deck: DeckId): void;
	resumeSlip(deck: DeckId): Promise<void>;
}

export type DeckRollSetLoop = (
	deck: DeckId,
	loop: { in_ms: number; out_ms: number } | null,
	beatLength: number | null
) => Promise<void>;

export function clearDeckRoll(deck: DeckId): void {
	_rollStates[deck] = null;
}

export function abandonDeckRoll(port: Pick<DeckRollPort, 'deckState'>, deck: DeckId): void {
	const state = _rollStates[deck];
	if (state === null) return;
	port.deckState(deck).slip_enabled = state.saved.slipEnabled;
	_rollStates[deck] = null;
}

export function deckRollQueryFields(deck: DeckId): {
	roll_beats: number | null;
	roll_stack: number[];
} {
	return rollQueryFields(_rollStates[deck]);
}

/** FXROLL-01: momentary beat loop that forces SLIP until release. */
export async function executeDeckRoll(
	port: DeckRollPort,
	setLoop: DeckRollSetLoop,
	deck: DeckId,
	beats: number | null
): Promise<void> {
	const st = port.deckState(deck);
	if (beats !== null) {
		assertRollBeats(beats);
		const rt = port.requireLoaded(deck, 'roll').rt as {
			processor: unknown;
			durationSec: number;
		};
		if (rt.processor === null || rt.durationSec <= 0 || st.stable_id === null) {
			throw new Error(`roll: deck ${deck} has no track loaded`);
		}
		if (!st.playing) {
			throw new Error(`roll: deck ${deck} is paused; a roll needs a playing deck`);
		}
		const grid = port.phaseGrid(st, 'roll');
		const currentMs = port.currentPosSec(deck) * 1000;
		const range = exactBeatLoopRangeMs(grid, currentMs, beats);
		const entry = { beats, in_ms: range.in_ms, out_ms: range.out_ms };
		const held = _rollStates[deck];
		if (held === null) {
			const saved = {
				slipEnabled: st.slip_enabled,
				slipActive: st.slip_active,
				loop: st.loop?.engaged ? { ...st.loop } : null
			};
			st.slip_enabled = true;
			try {
				await setLoop(deck, range, beats);
			} catch (error) {
				st.slip_enabled = saved.slipEnabled;
				throw error;
			}
			_rollStates[deck] = beginRoll(saved, entry);
			return;
		}
		const next = pushRoll(held, entry);
		await setLoop(deck, range, beats);
		_rollStates[deck] = next;
		return;
	}
	const plan = releaseRoll(_rollStates[deck]);
	if (plan === null) return;
	if (plan.kind === 'resume') {
		const { entry } = plan;
		await setLoop(deck, { in_ms: entry.in_ms, out_ms: entry.out_ms }, entry.beats);
		_rollStates[deck] = plan.state;
		return;
	}
	const restore = plan.restore;
	_rollStates[deck] = null;
	try {
		if (restore.loop !== null) {
			const { rt } = port.requireLoaded(deck, 'roll');
			await port.scheduleDeckIntoLoop(deck, rt, { ...restore.loop, engaged: true });
			if (!restore.slipActive) port.clearSlip(deck);
		} else if (st.slip_active) {
			await port.resumeSlip(deck);
		}
	} finally {
		st.slip_enabled = restore.slipEnabled;
	}
}
