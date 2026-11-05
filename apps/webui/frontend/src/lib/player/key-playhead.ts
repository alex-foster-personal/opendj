import { effectiveCamelotKey, keySyncKey, keySyncKeyIsGuess } from '$lib/player/key/camelot';
import type { AnlzData, AnlzKeySegment } from '$lib/rb/anlz-types';

export interface KeyAtPlayhead {
	display: string | null;
	title: string | null;
	markerTimesS: readonly number[];
}

function _segmentAtPlayhead(
	segments: readonly AnlzKeySegment[],
	positionS: number
): { current: AnlzKeySegment; next: AnlzKeySegment | null } | null {
	if (segments.length === 0) return null;
	for (let i = 0; i < segments.length; i++) {
		const seg = segments[i];
		if (positionS >= seg.start_s && positionS < seg.end_s) {
			return { current: seg, next: segments[i + 1] ?? null };
		}
		if (positionS < seg.start_s) {
			return { current: segments[0], next: seg };
		}
	}
	const last = segments[segments.length - 1];
	if (positionS >= last.start_s) return { current: last, next: null };
	return { current: segments[0], next: segments[1] ?? null };
}

export function keyAtPlayhead(
	anlz: AnlzData | null,
	positionMs: number,
	keyShiftSemitones: number,
	fallbackKey: string | null
): KeyAtPlayhead {
	if (anlz?.key_segments === undefined) {
		const display = effectiveCamelotKey(fallbackKey, keyShiftSemitones) ?? fallbackKey;
		return { display, title: null, markerTimesS: [] };
	}
	const block = anlz.key_segments;
	if (block.status !== 'ok') {
		// DECK-KEY-01: no per-segment keys (our analysis declined), so the deck shows
		// the track key the library shows (rekordbox / Mixed In Key / guess), never "--".
		const display = effectiveCamelotKey(fallbackKey, keyShiftSemitones) ?? fallbackKey ?? '--';
		return { display, title: null, markerTimesS: [] };
	}
	const segments = block.segments;
	if (segments.length <= 1) {
		const base = segments[0]?.key_camelot ?? fallbackKey;
		const display = effectiveCamelotKey(base, keyShiftSemitones) ?? base ?? '--';
		return { display, title: null, markerTimesS: [] };
	}
	const at = _segmentAtPlayhead(segments, positionMs / 1000);
	const base = at?.current.key_camelot ?? fallbackKey;
	const display = effectiveCamelotKey(base, keyShiftSemitones) ?? base ?? '--';
	let title: string | null = null;
	if (at?.next !== null && at?.next !== undefined) {
		const nextKey = effectiveCamelotKey(at.next.key_camelot, keyShiftSemitones) ?? at.next.key_camelot;
		title = `next key ${nextKey} at ${at.next.start_s.toFixed(1)}s`;
	}
	return { display, title, markerTimesS: segments.slice(1).map((seg) => seg.start_s) };
}

/** The deck fields the key readout reads. */
export interface DeckKeyFields {
	anlz: AnlzData | null;
	position_ms: number;
	key_shift_semitones: number;
	key: string | null;
	key_guess?: string | null;
}

/** DECK-KEY-01: the deck header's key at the playhead over the deck's resolved
 * key (keySyncKey), with isGuess set when that key is the "?" guess. */
export function deckKeyAtPlayhead(deck: DeckKeyFields): KeyAtPlayhead & { isGuess: boolean } {
	const at = keyAtPlayhead(deck.anlz, deck.position_ms, deck.key_shift_semitones, keySyncKey(deck));
	return { ...at, isGuess: keySyncKeyIsGuess(deck) };
}
