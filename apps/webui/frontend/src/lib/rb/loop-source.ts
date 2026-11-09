/**
 * DECKCTL-6235d (alpha status I14): the loop cluster's INT source selector.
 *
 * INT is the beatgrid: the readout and interval grid engage whole-beat loops
 * from where the playhead is. The other sources are the loops already stored
 * on the track (rekordbox djmdCue rows with an out point): hot-cue loops by
 * bank letter, and memory-cue loops, the one rekordbox marked active first.
 * Picking a stored loop engages exactly that range through the same `loop`
 * command an agent sends; the button then names the source in force.
 */
import type { AnlzCue } from './deck-state-types';

export interface StoredLoop {
	/** Stable key for the menu row. */
	id: string;
	source: 'hot_cue' | 'memory';
	/** Button text while this loop is engaged: "HOT A", "MEM". */
	short: string;
	/** Menu row text. */
	label: string;
	in_ms: number;
	out_ms: number;
	active: boolean;
	/** A memory loop can be flagged the track's active loop (CUES-9967). */
	canBeActive: boolean;
}

function _clock(ms: number): string {
	const totalSec = Math.max(0, ms / 1000);
	const min = Math.floor(totalSec / 60);
	const sec = totalSec - min * 60;
	return `${min}:${sec.toFixed(1).padStart(4, '0')}`;
}

/** Every stored loop on the track, hot-cue loops by bank letter first, then
 * memory loops in time order with rekordbox's active loop marked. */
export function storedLoops(cues: readonly AnlzCue[] | null | undefined): StoredLoop[] {
	if (cues === null || cues === undefined) return [];
	const hot: StoredLoop[] = [];
	const memory: StoredLoop[] = [];
	// Two cue rows can store the same range; the menu keys rows by id and the
	// picked row is remembered by id, so a repeat gets an occurrence suffix.
	const seen = new Map<string, number>();
	const uniqueId = (base: string): string => {
		const count = (seen.get(base) ?? 0) + 1;
		seen.set(base, count);
		return count === 1 ? base : `${base}-${count}`;
	};
	for (const cue of cues) {
		if (!cue.is_loop || cue.out_ms === null) continue;
		if (!Number.isFinite(cue.in_ms) || !Number.isFinite(cue.out_ms)) continue;
		if (cue.in_ms < 0 || cue.out_ms <= cue.in_ms) continue;
		const lengthSec = (cue.out_ms - cue.in_ms) / 1000;
		const span = `${_clock(cue.in_ms)}, ${lengthSec.toFixed(2)} s`;
		const comment = cue.comment !== null && cue.comment.trim() !== '' ? ` - ${cue.comment.trim()}` : '';
		if (cue.slot !== null) {
			hot.push({
				id: uniqueId(`hot-${cue.slot}-${cue.in_ms}`),
				source: 'hot_cue',
				short: `HOT ${cue.slot}`,
				label: `Hot cue ${cue.slot} loop (${span})${comment}`,
				in_ms: cue.in_ms,
				out_ms: cue.out_ms,
				active: cue.active_loop,
				canBeActive: false
			});
		} else {
			memory.push({
				id: uniqueId(`mem-${cue.in_ms}-${cue.out_ms}`),
				source: 'memory',
				short: 'MEM',
				label: `${cue.active_loop ? 'Active memory loop' : 'Memory loop'} (${span})${comment}`,
				in_ms: cue.in_ms,
				out_ms: cue.out_ms,
				active: cue.active_loop,
				canBeActive: true
			});
		}
	}
	hot.sort((a, b) => a.short.localeCompare(b.short) || a.in_ms - b.in_ms);
	memory.sort((a, b) => Number(b.active) - Number(a.active) || a.in_ms - b.in_ms);
	return [...hot, ...memory];
}

/** Within this many ms an engaged loop counts as the stored one. A stored
 * loop engages unquantized (`quantize: false`), so its ends come back as
 * saved; the slack only covers the Rust engine's frame rounding (well under
 * 1 ms). A grid loop a few ms off a stored one therefore still reads INT. */
export const STORED_LOOP_MATCH_MS = 1;

/** The stored loop the engaged loop is, or null when it is a grid loop. Two
 * stored loops can share one range (hot cue A and a memory loop); `pickedId`,
 * the menu row last picked, says which of them is in force. A loop with a
 * `beat_length` was engaged as INT (engageBeatLoop) and is never a stored
 * loop, even when its beat-aligned range equals one: a stored range engages
 * through the `loop` command, which leaves `beat_length` null. */
export function engagedStoredLoop(
	loops: readonly StoredLoop[],
	loop: { in_ms: number; out_ms: number; engaged: boolean; beat_length?: number | null } | null,
	pickedId: string | null = null
): StoredLoop | null {
	if (loop === null || !loop.engaged || (loop.beat_length ?? null) !== null) return null;
	const matches = loops.filter(
		(stored) =>
			Math.abs(stored.in_ms - loop.in_ms) <= STORED_LOOP_MATCH_MS &&
			Math.abs(stored.out_ms - loop.out_ms) <= STORED_LOOP_MATCH_MS
	);
	return matches.find((stored) => stored.id === pickedId) ?? matches[0] ?? null;
}

/** The menu's picked row after an engage or disengage: `next` when the engine
 * took the command, else `current`, so a refused command never relabels the
 * loop still in force. */
export async function pickedAfter(
	attempt: () => Promise<boolean>,
	current: string | null,
	next: string | null
): Promise<string | null> {
	return (await attempt()) ? next : current;
}

/** The picked row for the deck's current load: a pick made on an earlier
 * load (another track, or the same one reloaded) names nothing, since row
 * ids repeat across tracks. */
export function pickedForLoad(picked: { load: number; id: string | null }, load: number): string | null {
	return picked.load === load ? picked.id : null;
}

/** A value key for a deck's loop state, so a change is seen whether the
 * engine replaced the loop object or edited it in place. */
export function loopStateKey(
	loop: { in_ms: number; out_ms: number; engaged: boolean; beat_length?: number | null } | null
): string {
	return loop === null ? 'none' : `${loop.in_ms}:${loop.out_ms}:${loop.engaged}:${loop.beat_length ?? 'stored'}`;
}

/** The menu's pick once the live loop moves. It is marked `seen` the first
 * time the loop in force is the picked row, and dropped as soon as the loop
 * in force is anything else after that (exited from MIDI, the readout or an
 * agent, or moved). A pick not yet seen survives only while the deck's loop
 * state is still the one sampled BEFORE the command was sent (`since`, a
 * `loopStateKey`): the engage can apply on a later beat, but any change after
 * the command went out either is the pick or ends it, even when another
 * queued loop command ran before the pick's acknowledgement was handled. */
export function pickedWhileInForce<P extends { id: string | null; seen: boolean; since: string }>(
	picked: P,
	inForceId: string | null,
	loopNow: string
): P {
	if (picked.id === null) return picked;
	if (inForceId === picked.id) return picked.seen ? picked : { ...picked, seen: true };
	if (!picked.seen && loopNow === picked.since) return picked;
	return { ...picked, id: null, seen: false };
}
