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

/** Canvas geometry for one visible beat-grid line. The full-height line and
 * its top cap share an x/width so they remain one continuous marker. */
export interface BeatLineGeometry {
	x: number;
	y: 0;
	width: number;
	height: number;
	alpha: number;
	capHeight: number;
	capAlpha: number;
	isDownbeat: boolean;
}

/** Project ordered, real PQTZ beats into the visible canvas window.
 * Downbeats are wider and brighter; every returned line spans rowHeight. */
export function visibleBeatLines(
	beats: AnlzBeat[],
	tLeftSec: number,
	pxPerSec: number,
	widthCss: number,
	rowHeight: number
): BeatLineGeometry[] {
	if (beats.length === 0) return [];
	const tRightSec = tLeftSec + widthCss / pxPerSec;
	const lines: BeatLineGeometry[] = [];
	for (let i = firstBeatAtOrAfter(beats, tLeftSec); i < beats.length; i++) {
		const beat = beats[i];
		if (beat.t > tRightSec) break;
		const isDownbeat = beat.n === 1;
		lines.push({
			x: Math.round((beat.t - tLeftSec) * pxPerSec),
			y: 0,
			width: isDownbeat ? 2 : 1,
			height: rowHeight,
			alpha: isDownbeat ? 0.24 : 0.12,
			capHeight: isDownbeat ? 8 : 4,
			capAlpha: isDownbeat ? 1 : 0.55,
			isDownbeat
		});
	}
	return lines;
}

/** Gutter label like '1.1Bars' / '86.3Bars' - whole bars + leftover beats
 * until the next countdown target (SCREENSHOT-SPEC 2: bars until next
 * cue/phrase). Target picking, in priority order over REAL data only:
 *   1. earliest upcoming cue (memory / hot cue / loop-in), else
 *   2. earliest upcoming phrase boundary, else
 *   3. the end of the beatgrid (last analysed beat).
 * Most library tracks carry zero djmdCue rows, so without the phrase and
 * end-of-grid fallbacks the counter would almost never render - rekordbox
 * shows it whenever a beatgrid exists. Returns null only when the track
 * has no beatgrid or the playhead is past every target - never invented. */
export function barsToNextCueLabel(anlz: AnlzData, positionMs: number): string | null {
	const beats = anlz.beatgrid.beats;
	if (beats.length === 0) return null;
	const posS = positionMs / 1000;
	let targetS = Infinity;
	for (const cue of anlz.cues) {
		const cueS = cue.in_ms / 1000;
		if (cueS > posS && cueS < targetS) targetS = cueS;
	}
	if (!Number.isFinite(targetS)) {
		for (const phrase of anlz.phrases) {
			if (phrase.start_s > posS && phrase.start_s < targetS) targetS = phrase.start_s;
		}
	}
	if (!Number.isFinite(targetS)) {
		const gridEndS = beats[beats.length - 1].t;
		if (gridEndS > posS) targetS = gridEndS;
	}
	if (!Number.isFinite(targetS)) return null;
	const beatsRemaining = firstBeatAtOrAfter(beats, targetS) - firstBeatAtOrAfter(beats, posS);
	const bars = Math.floor(beatsRemaining / 4);
	const rem = beatsRemaining % 4;
	return `${bars}.${rem}Bars`;
}
