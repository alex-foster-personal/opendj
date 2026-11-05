/**
 * PAIR-03 frontend wiring over the durable pairing-capture routes (PAIR-01/02).
 * Plain module, not `.svelte.ts`: CreatePairingSheet.svelte cannot be mounted
 * in the unit-test harness, so the fetch-bearing logic lives here where it can
 * be loaded and tested directly (see load-typescript.mjs).
 */
import {
	createAlignment,
	createPairing,
	createSyncSnapshot,
	listSyncSnapshots,
	type SyncSnapshot
} from '$lib/api';
import type { HotCue, HotCueSlot } from '$lib/rb/hot-cue-types';

export { listPairingsFor } from '$lib/api';

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

/** The live deck fields a sync snapshot reads at Capture. */
export interface SyncSnapshotDeck {
	stable_id: string;
	/** The frozen position from the Create pairing sheet, in ms. */
	position_ms: number;
	/** Live playback rate ratio (DeckState.pitch); 1.0 is 0% pitch. */
	tempo_ratio: number;
	is_master: boolean;
}

/**
 * W19: the LV1 sync snapshot a Capture records for the (from, to) pair.
 * Before this, Capture only POSTed the pairing row, which is write-once
 * (PAIR-04): capturing a pair that was already saved changed nothing on the
 * server while the toast said "Pairing captured", and no Capture ever wrote
 * the sync snapshot that Reload sync replays. Side a is the from deck. The
 * master is whichever of the two is the elected master; with neither, the
 * from deck leads. BeatSyncMax bar-locks downbeats, so it records 'bar'.
 */
export function syncSnapshotBody(
	a: SyncSnapshotDeck,
	b: SyncSnapshotDeck,
	beatSyncMax: boolean
): {
	stable_a: string;
	stable_b: string;
	master_side: 'a' | 'b';
	sync_mode: 'bar' | 'beat';
	a_tempo_ratio: number;
	b_tempo_ratio: number;
	a_position_ms: number;
	b_position_ms: number;
} {
	for (const [side, deck] of [
		['a', a],
		['b', b]
	] as const) {
		if (!Number.isFinite(deck.position_ms) || deck.position_ms < 0) {
			throw new RangeError(
				`deck ${side} position_ms must be finite and >= 0, got ${deck.position_ms}`
			);
		}
	}
	return {
		stable_a: a.stable_id,
		stable_b: b.stable_id,
		master_side: b.is_master && !a.is_master ? 'b' : 'a',
		sync_mode: beatSyncMax ? 'bar' : 'beat',
		a_tempo_ratio: a.tempo_ratio,
		b_tempo_ratio: b.tempo_ratio,
		a_position_ms: a.position_ms,
		b_position_ms: b.position_ms
	};
}

/** Record one LV1 sync snapshot for a captured pair (append-only, PAIR-02). */
export async function recordSyncSnapshot(
	a: SyncSnapshotDeck,
	b: SyncSnapshotDeck,
	beatSyncMax: boolean
): Promise<SyncSnapshot> {
	return createSyncSnapshot(syncSnapshotBody(a, b, beatSyncMax));
}

/**
 * W19: one Capture. The pairing row first (write-once, PAIR-04: a saved pair
 * keeps its first snapshot), then the sync snapshot every Capture appends.
 * Lives here, not in performance-ipc, so the dispatcher keeps one import
 * edge for pairing capture (quality ratchet frontend.max_fan_out).
 */
export async function capturePairing(
	pairing: Parameters<typeof createPairing>[0],
	a: SyncSnapshotDeck,
	b: SyncSnapshotDeck,
	beatSyncMax: boolean
): Promise<void> {
	// Build (and validate) the snapshot first, so an invalid position fails
	// the Capture before the write-once pairing row is created.
	const snapshot = syncSnapshotBody(a, b, beatSyncMax);
	await createPairing(pairing);
	await createSyncSnapshot(snapshot);
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
