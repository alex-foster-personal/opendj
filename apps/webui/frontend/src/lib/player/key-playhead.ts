import { effectiveCamelotKey } from '$lib/player/key/camelot';
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
		const reason =
			typeof block.reason === 'string' && block.reason.length > 0
				? block.reason
				: `key segments ${block.status}`;
		return { display: '--', title: reason, markerTimesS: [] };
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
