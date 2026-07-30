/**
 * LV2 pairing alignment marks for the wavestack.
 * Fetches /pairings/alignments when both sides of a pair are loaded.
 */
import { RB_API_BASE } from '$lib/rb/api-rb';

export interface PairingAlignmentMark {
	id: string;
	stable_id: string;
	ms: number;
	label: string | null;
	peer_stable_id: string;
	peer_ms: number;
}

const _byStable = $state<Record<string, PairingAlignmentMark[]>>({});
const _inflight = new Set<string>();

export function alignmentMarksFor(stableId: string): PairingAlignmentMark[] {
	return _byStable[stableId] ?? [];
}

export function ensurePairingAlignments(stableA: string, stableB: string): void {
	const key = [stableA, stableB].sort().join('|');
	if (_inflight.has(key)) return;
	_inflight.add(key);
	const q = new URLSearchParams({ stable_a: stableA, stable_b: stableB });
	void fetch(`${RB_API_BASE}/api/v1/pairings/alignments?${q}`, {
		headers: { Accept: 'application/json' }
	})
		.then(async (r) => {
			if (!r.ok) throw new Error(`alignments HTTP ${r.status}`);
			const rows = (await r.json()) as Array<{
				id: string;
				stable_a: string;
				stable_b: string;
				anchor_a_ms: number;
				anchor_b_ms: number;
				label: string | null;
			}>;
			const marksA: PairingAlignmentMark[] = [];
			const marksB: PairingAlignmentMark[] = [];
			for (const row of rows) {
				marksA.push({
					id: row.id,
					stable_id: row.stable_a,
					ms: row.anchor_a_ms,
					label: row.label,
					peer_stable_id: row.stable_b,
					peer_ms: row.anchor_b_ms
				});
				marksB.push({
					id: `${row.id}:b`,
					stable_id: row.stable_b,
					ms: row.anchor_b_ms,
					label: row.label,
					peer_stable_id: row.stable_a,
					peer_ms: row.anchor_a_ms
				});
			}
			_byStable[stableA] = [
				...marksA.filter((m) => m.stable_id === stableA),
				...marksB.filter((m) => m.stable_id === stableA)
			];
			_byStable[stableB] = [
				...marksA.filter((m) => m.stable_id === stableB),
				...marksB.filter((m) => m.stable_id === stableB)
			];
		})
		.catch((err) => {
			console.error('pairing alignments fetch failed', err);
		})
		.finally(() => {
			_inflight.delete(key);
		});
}

export async function createHotcueAlignment(args: {
	stable_a: string;
	stable_b: string;
	slot_a: string;
	slot_b: string;
	ms_a: number;
	ms_b: number;
	label?: string;
}): Promise<void> {
	const r = await fetch(`${RB_API_BASE}/api/v1/pairings/alignments`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify({
			stable_a: args.stable_a,
			stable_b: args.stable_b,
			anchor_a_kind: 'hotcue',
			anchor_b_kind: 'hotcue',
			anchor_a_slot: args.slot_a,
			anchor_b_slot: args.slot_b,
			anchor_a_ms: args.ms_a,
			anchor_b_ms: args.ms_b,
			label: args.label ?? null
		})
	});
	if (!r.ok) throw new Error(`create alignment HTTP ${r.status}`);
	ensurePairingAlignments(args.stable_a, args.stable_b);
}
