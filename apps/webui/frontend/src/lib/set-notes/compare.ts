/**
 * SETNOTE-6340j: the pad's side panel arithmetic for the songs in the selection.
 * Plain numbers from library metadata; an unknown BPM or key stays unknown.
 */
import { camelotKeysAreCompatible, parseCamelotKey } from '$lib/player/key/camelot';

export type TrackFacts = { bpm?: number | null; key?: string | null };
export type Comparison = {
	deltaBpm: number | null;
	deltaPct: number | null;
	keysMix: boolean | null;
};

export function compareTracks(a: TrackFacts, b: TrackFacts): Comparison {
	const [ab, bb] = [a.bpm ?? null, b.bpm ?? null];
	const [ak, bk] = [a.key ?? null, b.key ?? null];
	const tempo = ab !== null && bb !== null && ab > 0;
	const keyed = parseCamelotKey(ak) !== null && parseCamelotKey(bk) !== null;
	return {
		deltaBpm: tempo ? bb - ab : null,
		deltaPct: tempo ? ((bb - ab) / ab) * 100 : null,
		keysMix: keyed ? camelotKeysAreCompatible(ak, bk) : null
	};
}

export function signed(n: number, digits = 1): string {
	return `${n > 0 ? '+' : ''}${n.toFixed(digits)}`;
}
