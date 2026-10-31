/**
 * CMDK-17..19 (issue #6092): the master deck's pairs in Cmd K, runeless so the
 * unit suite loads it.
 *
 * A pair is a pairing edge (`GET /api/v1/pairings`) between the master's track
 * and another. It is ALIGNED when the two have a captured sync snapshot
 * (`capture_repo.py`, `GET /api/v1/pairings/sync-snapshots`, newest first in
 * either order, `latestSyncSnapshot`), and LOOSE otherwise. An aligned pair
 * loads at its captured offset against the master as it plays now, with the
 * fader at 0; a loose pair loads like any track.
 */
import type { DeckId } from './deck-id';

/** The fields of a pairing edge the bar reads (`PairingOut`). */
export interface PairingEdgeRow {
	pairing_id: string;
	from_stable_id: string;
	to_stable_id: string;
	updated_at: string;
}

/**
 * The partners of `stable_id`, each once, most recently edited first. An edge
 * from a track to itself is not a pair.
 */
export function partnersOf(edges: readonly PairingEdgeRow[], stable_id: string): string[] {
	const latest = new Map<string, string>();
	for (const edge of edges) {
		let other: string | null = null;
		if (edge.from_stable_id === stable_id) other = edge.to_stable_id;
		else if (edge.to_stable_id === stable_id) other = edge.from_stable_id;
		if (other === null || other === stable_id) continue;
		const seen = latest.get(other);
		if (seen === undefined || edge.updated_at > seen) latest.set(other, edge.updated_at);
	}
	return [...latest.entries()].sort((a, b) => (a[1] < b[1] ? 1 : a[1] > b[1] ? -1 : 0)).map(([id]) => id);
}

/** A sync snapshot oriented master-first: `a` is the master's track, `b` the partner's. */
export interface PairSnapshot {
	aTempoRatio: number;
	bTempoRatio: number;
	aPositionMs: number;
	bPositionMs: number;
}

export interface CmdkPair {
	stable_id: string;
	/** The captured alignment; null for a loose pair. */
	snapshot: PairSnapshot | null;
}

/** The master deck as it plays now. */
export interface MasterNow {
	position_ms: number;
	/** Playback rate ratio (1.0 = 0%). */
	ratio: number;
	playing: boolean;
}

export type AlignedPlan =
	| { ok: true; position_ms: number; ratio: number; play: boolean }
	| { ok: false; reason: string };

/**
 * Where the partner goes to sit against the master as captured, and at what
 * rate. Track time on each deck advances at its own rate, so a step of `dm`
 * on the master is `dm * bRatio / aRatio` on the partner at the captured
 * rates. The partner's rate keeps the captured ratio between the two when the
 * master has since moved its pitch. It plays (fader at 0) when the master
 * does, so it stays in phase until the DJ brings it in.
 *
 * Refuses, with the reason in words, when that point is off the partner's
 * track (the master is before the captured point by more than the partner
 * has, or so far past it that the partner would be over), or the snapshot's
 * rates are not positive. A partner whose length is not known refuses too:
 * without it the end of the overlap cannot be checked, and a track read as
 * unbounded would plan a load past where the partner actually ends (Sol P1).
 */
export function alignedPlan(snap: PairSnapshot, master: MasterNow, partnerDurationMs: number | null): AlignedPlan {
	if (!(snap.aTempoRatio > 0) || !(snap.bTempoRatio > 0) || !(master.ratio > 0)) {
		return { ok: false, reason: 'the captured tempo is not usable' };
	}
	if (partnerDurationMs === null) {
		return { ok: false, reason: 'the partner track has no known length, so its overlap cannot be checked' };
	}
	const position = snap.bPositionMs + (master.position_ms - snap.aPositionMs) * (snap.bTempoRatio / snap.aTempoRatio);
	if (position < 0) {
		const ahead = Math.round((-position * snap.aTempoRatio) / snap.bTempoRatio / 1000);
		return { ok: false, reason: `the master is ${ahead} s before the captured point` };
	}
	if (position >= partnerDurationMs) {
		return { ok: false, reason: 'the master is past the end of the captured overlap' };
	}
	return {
		ok: true,
		position_ms: Math.round(position),
		ratio: snap.bTempoRatio * (master.ratio / snap.aTempoRatio),
		play: master.playing
	};
}

/** The dispatcher commands an aligned load sends around the load itself. */
export type PairCommand =
	| { type: 'fader'; deck: DeckId; value: number }
	| { type: 'beat_sync'; deck: DeckId; enabled: false }
	| { type: 'tempo'; deck: DeckId; ratio: number }
	| { type: 'seek'; deck: DeckId; position_ms: number; exact: true }
	| { type: 'play'; deck: DeckId; playing: boolean };

/** Before the load: the target's fader to 0, so nothing is heard until the DJ brings it in. */
export function beforeAlignedLoad(deck: DeckId): PairCommand[] {
	return [{ type: 'fader', deck, value: 0 }];
}

/**
 * Whether a partner rate fits the deck's selected pitch range (`rangePct`, e.g. 8 for +-8%),
 * the same bound the engine's `setTempoRatio` refuses outside of. Checked before the fader
 * moves, so a pair the deck cannot play at its captured rate never replaces what is on it.
 */
export function ratioInRange(ratio: number, rangePct: number): boolean {
	return ratio > 0 && Math.abs(ratio - 1) * 100 <= rangePct + 1e-9;
}

/**
 * After the load: Beat Sync off on the partner, then its rate. With Beat Sync on, play
 * re-phases a follower to the master's grid and overwrites the captured rate and offset
 * (Codex r4276826010), so the captured alignment takes the deck off Beat Sync. The offset follows (`alignedSeek`),
 * recomputed against the master at that moment, and only then play
 * (`alignedPlay`), so the paused partner is already at its offset when it
 * starts: a fader raised while play is on its way never makes the load point
 * heard (Codex r4276688934). The corrective seeks then take up the start latency.
 */
export function afterAlignedLoad(deck: DeckId, plan: Extract<AlignedPlan, { ok: true }>): PairCommand[] {
	return [
		{ type: 'beat_sync', deck, enabled: false },
		{ type: 'tempo', deck, ratio: plan.ratio }
	];
}

/** The start of an aligned load, sent after its offset seek, and only when the master plays. */
export function alignedPlay(deck: DeckId, plan: Extract<AlignedPlan, { ok: true }>): PairCommand | null {
	return plan.play ? { type: 'play', deck, playing: true } : null;
}

/**
 * The last command of an aligned load: the partner to its captured offset against the master now.
 * Exact, never snapped to the partner's grid: quantize would keep the partner's phase from
 * whatever point it loaded at, which is not the captured offset.
 */
export function alignedSeek(deck: DeckId, plan: Extract<AlignedPlan, { ok: true }>): PairCommand {
	return { type: 'seek', deck, position_ms: plan.position_ms, exact: true };
}

/** How long after the seek the deck state is read back to measure its lag (a few state ticks). */
export const SEEK_SETTLE_MS = 150;
/** A lag under this is inside one audio callback or two and is left alone. */
export const SEEK_LEAD_FLOOR_MS = 15;
/** A lag over this is not seek latency (the deck stalled or moved on) and is not chased. */
export const SEEK_LEAD_CEIL_MS = 500;

/** Corrective seeks after the first, at most. */
export const SEEK_CORRECTIONS = 2;

/**
 * Whether the partner reads back further from its offset than any seek latency: the deck did
 * not follow the seek (an overloaded engine, a stall), so the pair is reported off rather than
 * taken as aligned, which `leadFor` alone would read as nothing to correct.
 */
export function seekStalled(wantMs: number, actualMs: number): boolean {
	return Math.abs(Math.round(wantMs - actualMs)) > SEEK_LEAD_CEIL_MS;
}

/**
 * The partner's lag behind where it belongs (positive) or lead over it
 * (negative) right after a seek, when that is seek latency: inside the floor
 * it is already aligned, past the ceiling it stalled or moved on, and both
 * read 0, nothing to correct.
 */
export function leadFor(wantMs: number, actualMs: number): number {
	const lag = Math.round(wantMs - actualMs);
	const size = Math.abs(lag);
	return size > SEEK_LEAD_FLOOR_MS && size <= SEEK_LEAD_CEIL_MS ? lag : 0;
}

/** A bar entry's identity: the track (a pair row apart from the same track elsewhere) or the action. */
export function cmdkEntryKey(entry: { kind: 'track'; row: { stable_id: string }; pair?: unknown } | { kind: 'action'; action: { id: string } }): string {
	return entry.kind === 'action' ? `a:${entry.action.id}` : `${entry.pair === undefined ? 't' : 'p'}:${entry.row.stable_id}`;
}

/**
 * What an aligned pair was read against: the master's track and the captured snapshot. A pick
 * read under one master must not land on the same partner's alignment after the master moved,
 * so the wire id carries this and a changed basis refuses the stale pick (Codex P2 BLOCKING, #6691).
 */
export function pairBasis(master: string, masterDeck: number, snap: PairSnapshot): string {
	// The master's deck is part of the basis: the same track on two decks is two alignment sources (Codex P2 BLOCKING, #6691).
	return JSON.stringify([masterDeck, master, snap.aTempoRatio, snap.bTempoRatio, snap.aPositionMs, snap.bPositionMs]);
}

/**
 * The row id `query().cmdk` reports and `cmdk_pick {id}` pins (CMDK-19): a master's pair row is
 * `pair:<stable_id>:loose` or `pair:<stable_id>:aligned:<basis>`, never the bare stable_id the same track has elsewhere, because picking it
 * runs an aligned load rather than a plain one. A pick read against the plain row is refused once
 * the pair row takes its index (Codex r4279717126).
 */
export function cmdkWireId(
	entry:
		| { kind: 'track'; row: { stable_id: string }; pair?: { aligned: boolean; basis?: string | undefined } | undefined }
		| { kind: 'action'; action: { id: string; value?: unknown; next?: unknown; command?: unknown } }
): string {
	if (entry.kind === 'action') {
		const { id, value, next, command } = entry.action;
		// A setting's id carries the value it shows and the value Enter writes, so a pick read before either moved is
		// refused: an enum's target is named by the query, not by the value alone (Codex P2 BLOCKING, #6691).
		if (value !== undefined) return `${id}=${JSON.stringify(value)}${next === undefined ? '' : `>${JSON.stringify(next)}`}`;
		// A command that carries a query (Open settings for "x") does what its query says, so the query is its identity
		// (Codex P2 BLOCKING, #6691).
		if (typeof command === 'object' && command !== null && 'query' in command) return `${id}=${JSON.stringify(command.query)}`;
		return id;
	}
	// The load mode is part of a pair's identity: a pick read as a loose row must not land on the aligned one after a
	// refresh turned it over (Codex P2 BLOCKING, #6691). An aligned pair also names its basis (see pairBasis).
	if (entry.pair === undefined) return entry.row.stable_id;
	if (!entry.pair.aligned) return `pair:${entry.row.stable_id}:loose`;
	return `pair:${entry.row.stable_id}:aligned:${entry.pair.basis ?? ''}`;
}

/**
 * Where the highlight belongs once rows arrived around it with the query unchanged (the pairs
 * group lands after the bar opens): on the same entry wherever it moved, so Enter never acts on
 * a row the DJ did not choose; where it was when that entry is gone (Codex r4277873858).
 */
export function reanchorSelection(anchor: string | null, keys: readonly string[], at: number): number {
	if (anchor === null) return at;
	const found = keys.indexOf(anchor);
	return found >= 0 ? found : at;
}
