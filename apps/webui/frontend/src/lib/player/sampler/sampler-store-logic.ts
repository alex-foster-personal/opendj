/**
 * Edge sampler store: the pure parts ((private path) 7.4, 10; issue #11102).
 * No runes, no DOM, no fetch: the store core feeds state in.
 *
 * Requirements (mini-PRD):
 *   ✔︎ refs resolve to the slot on screen: `S<set>-<nn>` and `R-<nn>`, 1-based
 *     [if] ref is 'S2-03' [then] it is set index 1, slot index 2 [else ⛔️]
 *     [if] ref names an empty slot or a set that does not exist [then] null [else ⛔️]
 *   ✔︎ the compat sort hands the master BPM and key to the server, and only when known
 *     [if] there is no master reference [then] the query carries no master_bpm/master_key [else ⛔️]
 *   ✔︎ a set with a user-owned layout (server meta slot_ids) keeps it; the rest are computed
 *     [if] set-1 has slot_ids [then] set-1 shows exactly those, and set-2 starts with the first unplaced slot [else ⛔️]
 *   ✔︎ a failed slot patch rolls back and rethrows
 *     [if] the PATCH rejects [then] the slot is back to its old value and the error reaches the caller [else ⛔️]
 *   ✔︎ the compatible verdict uses the library's own rules and prefs object
 *     [if] the library's prefs narrow the BPM window [then] the sampler's verdict changes with it [else ⛔️]
 *   ✔︎ stems pending: captured stem settings change the sound but the track has no stem-set yet
 *     [if] bass is muted and stems_ready is false [then] stemsPending is true [else ⛔️]
 *   → the Starter set (spec 3.3) lands with the starter rows; starter slots are kept out of user sets
 */

import {
	compatibleRowKeyShift,
	isAppropriateNext,
	type NextOnlyRef
} from '$lib/rb/next-only-filter';
import type { CompatibleFilterPrefs } from '$lib/rb/compatible-filter-prefs';
import type { SlotPatch, SlotsQuery } from './api-sampler';
import type { SearchableSlot } from './edge-search';
import type { SampleSet } from './sampler-sets';
import { regionBars, type SamplerSlot, type SetMeta } from './sampler-types';

/** The master re-rank runs at most this often. */
export const RERANK_MIN_MS = 5000;

//----- refs

export type ParsedRef =
	{ kind: 'set'; set: number; index: number } | { kind: 'results'; index: number };

/** Parse `S<set>-<nn>` or `R-<nn>` (1-based) into 0-based indexes; null when malformed. */
export function parseRef(ref: string): ParsedRef | null {
	const m = /^(?:S(\d+)|R)-(\d{2,})$/.exec(ref.trim());
	if (m === null) return null;
	const index = Number(m[2]) - 1;
	if (index < 0) return null;
	if (m[1] === undefined) return { kind: 'results', index };
	const set = Number(m[1]) - 1;
	return set < 0 ? null : { kind: 'set', set, index };
}

/** The slot a ref names in these sets or search results; null for a missing set or empty slot. */
export function resolveRefIn<T>(
	ref: string,
	sets: readonly SampleSet<T>[],
	results: readonly T[] | null
): T | null {
	const p = parseRef(ref);
	if (p === null) return null;
	if (p.kind === 'results') return results?.[p.index] ?? null;
	return sets[p.set]?.items[p.index] ?? null;
}

//----- compat sort hand-off

/** GET /slots query for the compat sort; `q` asks the server for matches across all sets. */
export function slotsQueryFor(
	master: NextOnlyRef | null,
	offset: number,
	limit: number,
	q = ''
): SlotsQuery {
	const query: SlotsQuery = { sort: 'compat', q, offset, limit };
	if (master?.bpm != null) query.master_bpm = Math.round(master.bpm * 100) / 100;
	if (master?.key != null) query.master_key = master.key;
	return query;
}

/** Whether two master references rank the same (same key, BPM within 0.05). */
export function sameMaster(a: NextOnlyRef | null, b: NextOnlyRef | null): boolean {
	if (a === null || b === null) return a === b;
	return a.key === b.key && Math.abs((a.bpm ?? 0) - (b.bpm ?? 0)) < 0.05;
}

/** ms to wait before re-ranking for a new master: 0 = now. */
export function rerankDelay(lastRankAt: number, now: number): number {
	return Math.max(0, lastRankAt + RERANK_MIN_MS - now);
}

//----- sets layout

/**
 * Sets in order `set-1`, `set-2`, ...: a set whose server meta has `slot_ids` keeps that
 * layout (unknown ids become empty slots); every other set takes the next `perSet`
 * slots of the ranked catalogue that no user layout holds. Starter slots are left out.
 */
export function layoutSets(
	ranked: readonly SamplerSlot[],
	perSet: number,
	metas: readonly SetMeta[]
): SampleSet<SamplerSlot>[] {
	if (!(perSet > 0)) throw new Error(`layoutSets: perSet must be positive, got ${perSet}`);
	const own = ranked.filter((s) => s.source !== 'starter');
	const byId = new Map(own.map((s) => [s.slot_id, s]));
	const metaById = new Map(metas.map((m) => [m.id, m]));
	const placed = new Set(
		metas.flatMap((m) => m.slot_ids ?? []).filter((id): id is string => id !== null)
	);
	const free = own.filter((s) => !placed.has(s.slot_id));
	const lastUserSet = Math.max(
		0,
		...metas.filter((m) => m.slot_ids !== null).map((m) => setNumber(m.id))
	);
	const sets: SampleSet<SamplerSlot>[] = [];
	let next = 0;
	for (let n = 1; next < free.length || n <= lastUserSet; n++) {
		const id = `set-${n}`;
		const meta = metaById.get(id);
		const items: Array<SamplerSlot | null> =
			meta?.slot_ids != null
				? meta.slot_ids.map((sid) => (sid === null ? null : (byId.get(sid) ?? null)))
				: free.slice(next, (next += perSet));
		sets.push({ id, name: meta?.name || `Set ${n}`, description: meta?.description ?? '', items });
	}
	return sets;
}

function setNumber(id: string): number {
	const m = /^set-(\d+)$/.exec(id);
	return m === null ? 0 : Number(m[1]);
}

/** A set's slot ids in order, null for an empty slot (the PUT /sets body). */
export const setSlotIds = (s: SampleSet<SamplerSlot>): Array<string | null> =>
	s.items.map((x) => x?.slot_id ?? null);

/** Sets whose slot list differs between two layouts (by id), for the PUTs after a drop. */
export function changedSets(
	before: readonly SampleSet<SamplerSlot>[],
	after: readonly SampleSet<SamplerSlot>[]
): SampleSet<SamplerSlot>[] {
	return after.filter(
		(s, i) => before[i] === undefined || setSlotIds(before[i]).join('|') !== setSlotIds(s).join('|')
	);
}

//----- optimistic patch

/** The slot as it reads once `patch` lands (an omitted field keeps its value). */
export function applySlotPatch(slot: SamplerSlot, patch: SlotPatch): SamplerSlot {
	return {
		...slot,
		...(patch.name !== undefined ? { name: patch.name } : {}),
		...(patch.description !== undefined ? { description: patch.description } : {}),
		settings: patch.settings ?? slot.settings
	};
}

/** Apply at once, send, keep the server's copy; on failure restore the old slot and rethrow. */
export async function patchWithRollback(
	get: (id: string) => SamplerSlot | null,
	put: (slot: SamplerSlot) => void,
	id: string,
	patch: SlotPatch,
	send: (id: string, patch: SlotPatch) => Promise<SamplerSlot>
): Promise<SamplerSlot> {
	const before = get(id);
	if (before === null) throw new Error(`updateSlot: no slot ${id}`);
	put(applySlotPatch(before, patch));
	try {
		const saved = await send(id, patch);
		put(saved);
		return saved;
	} catch (err) {
		put(before);
		throw err;
	}
}

/** The pin list with `id` pinned (appended once) or unpinned. */
export function withPin(pins: readonly string[], id: string, pinned: boolean): string[] {
	const rest = pins.filter((x) => x !== id);
	return pinned ? [...rest, id] : rest;
}

//----- compatibility against the master

export interface KeyPlan {
	key: string | null;
	shift: number;
	/** 0 same key as master, 1 compatible (maybe shifted), 2 not compatible */
	dist: 0 | 1 | 2;
}

/** The key a slot plays in against the master, its shift, and how far it is. */
export function keyPlanFor(
	slot: SamplerSlot,
	master: NextOnlyRef | null,
	prefs: CompatibleFilterPrefs
): KeyPlan {
	if (master === null || master.key === null || slot.key === null)
		return { key: slot.key, shift: 0, dist: 2 };
	const plan = compatibleRowKeyShift(slot.key, master.key, prefs);
	if (plan === null) return { key: slot.key, shift: 0, dist: 2 };
	return { key: plan.key, shift: plan.shift, dist: plan.key === master.key ? 0 : 1 };
}

/** The library's "compatible" verdict for a slot against the master (false with no master). */
export function slotIsCompatible(
	slot: SamplerSlot,
	master: NextOnlyRef | null,
	prefs: CompatibleFilterPrefs
): boolean {
	return (
		master !== null &&
		isAppropriateNext({ key: slot.key, bpm: slot.native_bpm, energy: slot.energy }, master, prefs)
	);
}

/** Whether only the BPM part of the library's prefs passes (true with no master). */
export function bpmIsCompatible(
	slot: SamplerSlot,
	master: NextOnlyRef | null,
	prefs: CompatibleFilterPrefs
): boolean {
	if (master === null) return true;
	return isAppropriateNext(
		{ key: slot.key, bpm: slot.native_bpm },
		{ key: null, bpm: master.bpm },
		{ ...prefs, rules: [], mode: 'transition' }
	);
}

/** The search view of a slot (edge-search), with its compatible verdict. */
export function toSearchable(slot: SamplerSlot, compatible: boolean): SearchableSlot {
	return {
		title: slot.title,
		name: slot.name,
		cueLabel: cueLabel(slot),
		description: slot.description,
		key: slot.key ?? '',
		broken: slot.flags.broken,
		remix: slot.flags.remix,
		vocals: slot.flags.vocals,
		offline: slot.flags.offline,
		compatible
	};
}

/** The meta line's cue label: `A 4 bars`, `loop 2 bars` for a capture, else just the length. */
export function cueLabel(slot: Pick<SamplerSlot, 'cue_letter' | 'beats' | 'source'>): string {
	const bars = regionBars(slot);
	const len = `${bars} ${bars === 1 ? 'bar' : 'bars'}`;
	if (slot.source === 'capture') return `loop ${len}`;
	return slot.cue_letter ? `${slot.cue_letter} ${len}` : len;
}

/** Captured stem settings change the sound but the track has no stem-set yet, so its mix clip plays (EDGESAMP-11872h). */
export function stemsPending(slot: Pick<SamplerSlot, 'vector' | 'stems_ready'>): boolean {
	const stems = slot.vector?.stems;
	if (!stems || slot.stems_ready) return false;
	return Object.values(stems).some((p) => p.gain_db !== 0 || p.mute || p.solo);
}
