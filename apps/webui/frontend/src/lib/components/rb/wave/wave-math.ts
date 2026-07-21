/**
 * Pure time/beat math for the wavestack rows (build unit: wavestack).
 * No runes, no DOM - unit-testable helpers only.
 */
import type { AnlzBeat, AnlzData } from '$lib/rb/types';

/** Index of the FIRST beat with t >= tSec (== beats.length when none).
 * beats MUST be ordered by t (contract: AnlzBeatgrid.beats is ordered). */
export function firstBeatAtOrAfter(beats: AnlzBeat[], tSec: number): number {
	let lo = 0;
	let hi = beats.length;
	while (lo < hi) {
		const mid = (lo + hi) >> 1;
		if (beats[mid].t < tSec) {
			lo = mid + 1;
		} else {
			hi = mid;
		}
	}
	return lo;
}

/** Gutter label like '1.1Bars' / '86.3Bars' - whole bars + leftover beats
 * until the next upcoming cue (memory, hot cue or loop-in).
 * Returns null when the track has no beatgrid or no upcoming cue - the
 * label is hidden in that case (COMPONENT-MAP 1.2), never invented. */
export function barsToNextCueLabel(anlz: AnlzData, positionMs: number): string | null {
	const beats = anlz.beatgrid.beats;
	if (beats.length === 0) return null;
	const posS = positionMs / 1000;
	let nextCueS = Infinity;
	for (const cue of anlz.cues) {
		const cueS = cue.in_ms / 1000;
		if (cueS > posS && cueS < nextCueS) nextCueS = cueS;
	}
	if (!Number.isFinite(nextCueS)) return null;
	const beatsRemaining = firstBeatAtOrAfter(beats, nextCueS) - firstBeatAtOrAfter(beats, posS);
	const bars = Math.floor(beatsRemaining / 4);
	const rem = beatsRemaining % 4;
	return `${bars}.${rem}Bars`;
}
