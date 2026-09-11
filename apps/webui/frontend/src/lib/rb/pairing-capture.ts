/**
 * PAIR-03 frontend wiring over the durable pairing-capture routes (PAIR-01/02).
 * Plain module, not `.svelte.ts`: CreatePairingSheet.svelte cannot be mounted
 * in the unit-test harness, so the fetch-bearing logic lives here where it can
 * be loaded and tested directly (see load-typescript.mjs).
 */
import { createAlignment, listSyncSnapshots } from '$lib/api';
import type { HotCue, HotCueSlot } from '$lib/rb/hot-cue-types';

const HOT_CUE_SLOTS: HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

export interface ResolvedSyncSnapshot {
	masterIsA: boolean;
	aTempoRatio: number;
	bTempoRatio: number;
	aPositionMs: number;
	bPositionMs: number;
}

/**
 * Newest sync snapshot for a pair, re-mapped onto (stableA, stableB) order.
 *
 * The repo's `list_snapshots` matches `stable_a`/`stable_b` directionally
 * (exact columns, no either-side OR the way alignments get), so the same two
 * decks re-picked in the opposite order at capture time would otherwise miss
 * a real snapshot. Query both orders and re-map whichever is newest, rather
 * than replaying whichever row a single directional query happens to find.
 */
export async function latestSyncSnapshot(
	stableA: string,
	stableB: string
): Promise<ResolvedSyncSnapshot | null> {
	const [direct, reversed] = await Promise.all([
		listSyncSnapshots(stableA, stableB, 1),
		listSyncSnapshots(stableB, stableA, 1)
	]);
	const a = direct[0];
	const b = reversed[0];
	if (a === undefined && b === undefined) return null;
	if (a !== undefined && (b === undefined || a.captured_at >= b.captured_at)) {
		return {
			masterIsA: a.master_side === 'a',
			aTempoRatio: a.a_tempo_ratio,
			bTempoRatio: a.b_tempo_ratio,
			aPositionMs: a.a_position_ms,
			bPositionMs: a.b_position_ms
		};
	}
	const row = b as NonNullable<typeof b>;
	return {
		masterIsA: row.master_side === 'b',
		aTempoRatio: row.b_tempo_ratio,
		bTempoRatio: row.a_tempo_ratio,
		aPositionMs: row.b_position_ms,
		bPositionMs: row.a_position_ms
	};
}

export interface HotcueAlignmentResult {
	paired: number;
}

/** Alignment capture is append-only POST-per-slot, so a failure partway
 * through leaves the earlier POSTs durably written. Carries how many landed
 * so the caller can report an honest partial result instead of a bare error. */
export class PartialAlignmentError extends Error {
	constructor(
		public readonly paired: number,
		cause: unknown
	) {
		super(`alignment failed after pairing ${paired} hot-cue slot(s): ${String(cause)}`);
		this.name = 'PartialAlignmentError';
	}
}

/** POSTs one durable alignment mark per hot-cue slot letter present on both decks. */
export async function alignHotcues(
	stableA: string,
	stableB: string,
	hotCuesA: HotCue[],
	hotCuesB: HotCue[]
): Promise<HotcueAlignmentResult> {
	let paired = 0;
	for (const slot of HOT_CUE_SLOTS) {
		const ha = hotCuesA.find((cue) => cue.slot === slot);
		const hb = hotCuesB.find((cue) => cue.slot === slot);
		if (ha === undefined || hb === undefined) continue;
		try {
			await createAlignment({
				stable_a: stableA,
				stable_b: stableB,
				anchor_a_kind: 'hotcue',
				anchor_b_kind: 'hotcue',
				anchor_a_slot: slot,
				anchor_b_slot: slot,
				anchor_a_ms: ha.in_ms,
				anchor_b_ms: hb.in_ms,
				label: `HC ${slot}`
			});
		} catch (exc) {
			throw new PartialAlignmentError(paired, exc);
		}
		paired += 1;
	}
	return { paired };
}
