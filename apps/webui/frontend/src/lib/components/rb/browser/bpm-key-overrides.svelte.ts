/**
 * Edits from the browser tempo and key cells (pin a720a02ecf92).
 * A key edit is stored with the track (KEY-10229, PUT /tracks/{id}/key-edit);
 * this store is a thin cache that bridges the commit until the row re-reads
 * and serves key_source "edit". BPM edits stay session-only until #10112 adds
 * PUT /grid-edit {bpm}. When that track is on a deck, the same commit sets the
 * deck's playback tempo or nudges its key so the new value is audible.
 */
import type { PitchRange } from '$lib/player/constants';
import { effectiveCamelotKey } from '$lib/player/key/camelot';
import { playbackBpm } from '$lib/rb/beat-sync-math';
import { deleteKeyEdit, putKeyEdit } from '$lib/rb/beatgrid-fallback-api';
import type { DeckId } from '$lib/rb/deck-slots';
import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';
import { tempoWritesFromTargetBpm } from '$lib/rb/tempo-edit';
import { semitoneNudgeBetween } from './bpm-key-cell-edit';

/** The live decks, handed in by the table (which already reads the engine),
 * so this module adds no importer to the audio engine. */
export interface CellEditDecks {
	ids: readonly DeckId[];
	states: Record<
		DeckId,
		{
			stable_id: string | null;
			position_ms: number;
			bpm: number | null;
			key: string | null;
			key_shift_semitones: number;
			anlz: { beatgrid: { beats: NonNullable<Parameters<typeof playbackBpm>[0]['beats']> } } | null;
		}
	>;
	pitchRanges: Record<DeckId, PitchRange>;
}

let bpmEdits = $state<Record<string, number>>({});
let keyEdits = $state<Record<string, string>>({});

export function hasBpmEdit(stableId: string): boolean {
	return bpmEdits[stableId] !== undefined;
}

export function hasKeyEdit(stableId: string): boolean {
	return keyEdits[stableId] !== undefined;
}

export function editedBpm(stableId: string, fallback: number | null): number | null {
	const edited = bpmEdits[stableId];
	return edited === undefined ? fallback : edited;
}

export function editedKey(stableId: string, fallback: string | null): string | null {
	const edited = keyEdits[stableId];
	return edited === undefined ? fallback : edited;
}

export async function commitBpmEdit(
	stableId: string,
	bpm: number,
	decks: CellEditDecks
): Promise<void> {
	bpmEdits = { ...bpmEdits, [stableId]: bpm };
	for (const deck of decks.ids) {
		const state = decks.states[deck];
		if (state.stable_id !== stableId) continue;
		const base = playbackBpm({
			beats: state.anlz?.beatgrid.beats,
			positionSec: Math.max(0, state.position_ms / 1000),
			tempoRatio: 1,
			tagBpm: state.bpm
		});
		if (base === null) continue;
		const writes = tempoWritesFromTargetBpm({
			deck,
			targetBpm: bpm,
			baseBpm: base,
			currentRange: decks.pitchRanges[deck]
		});
		if (writes === null) continue;
		// The cell already shows the edit. A refused write (toasted by the dispatcher) stops the rest,
		// so the deck is not left half way through a range change plus tempo.
		for (const write of writes) {
			if (!(await runPerformanceCommandFromUi(write)).ok) break;
		}
	}
}

export async function commitKeyEdit(
	stableId: string,
	label: string,
	decks: CellEditDecks
): Promise<void> {
	// A refused store throws to the editor, which shows it; nothing is cached or nudged.
	const saved = await putKeyEdit(stableId, label);
	if (saved.key === null) throw new Error(`key edit for ${stableId} stored no key`);
	const key = saved.key;
	keyEdits = { ...keyEdits, [stableId]: key };
	for (const deck of decks.ids) {
		const state = decks.states[deck];
		if (state.stable_id !== stableId || state.key === null) continue;
		const current = effectiveCamelotKey(state.key, state.key_shift_semitones);
		const delta = semitoneNudgeBetween(current, key);
		if (delta === null || delta === 0) continue;
		const next = state.key_shift_semitones + delta;
		if (next < -12 || next > 12) continue;
		// key_nudge moves one semitone per command on main, so a wider step
		// is sent as that many single nudges in the same direction.
		const step: 1 | -1 = delta > 0 ? 1 : -1;
		// The cell already shows the key. A refused nudge (toasted by the dispatcher) stops the rest.
		for (let i = 0; i < Math.abs(delta); i++) {
			if (!(await runPerformanceCommandFromUi({ type: 'key_nudge', deck, semitones: step })).ok)
				break;
		}
	}
}

/** Forget the track's stored key edit (the analysed key shows again). */
export async function clearKeyEdit(stableId: string): Promise<void> {
	await deleteKeyEdit(stableId);
	const { [stableId]: _dropped, ...rest } = keyEdits;
	keyEdits = rest;
}
