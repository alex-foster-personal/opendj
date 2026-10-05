/**
 * One rule for "Beat Sync may wander on this beatgrid" (GRIDFLAG-01).
 *
 * The deck's Beat Sync badge and the library's Err-column flag both read this
 * rule. It is the TypeScript mirror of
 * `apps/analysis_beatgrid/grid_quality.py`, which documents the rule in full:
 *
 *   1. FLAG a grid when any beat interval is more than 3 ms from the grid's
 *      median interval.
 *   2. CLASSIFY a flagged grid with the constant-region recipe run on its own
 *      beat times: `suspect` (steady music under an uneven grid) when the
 *      steady span covers at least half the track AND at least 70% of all
 *      beats sit on that one line; otherwise `variable_tempo`.
 *   3. Fewer than two beats is `unknown`, never ok and never bad.
 *
 * There is no shared runtime between the two languages, so
 * `tests/fixtures/grid_quality_conformance.json` pins both: each side asserts
 * its thresholds equal the fixture's and that every fixture grid produces the
 * fixture's class, numbers and message (`grid-quality.test.mjs`).
 *
 * The library never calls `classifyGrid`: its rows carry the verdict the
 * server stored. Only the deck does, once per loaded grid.
 *
 * Requirements:
 *   ✔︎ ✅ 🎯 classifyGrid(): ok / suspect / variable_tempo / unknown.
 *     [if] a fixed-tempo grid stored to the millisecond [then] ok ⛔️ flagged
 *     [if] a steady grid has one beat moved 4 ms [then] suspect ⛔️ variable
 *     [if] the tempo ramps through the track [then] variable_tempo ⛔️ suspect
 *     [if] fewer than two beats [then] unknown ⛔️ ok or suspect
 *   ✔︎ ✅ 🎯 gridQualityMessage(): same sentence as the Python side, to the byte.
 */

export type GridClass = 'ok' | 'suspect' | 'variable_tempo' | 'unknown';

/** The classes a user is warned about (and may dismiss). */
export const FLAGGED_GRID_CLASSES: readonly GridClass[] = ['suspect', 'variable_tempo'];

/** Every number the rule compares against. Mirrors `THRESHOLDS` in
 * grid_quality.py; the conformance test fails when the two differ. */
export const GRID_QUALITY_THRESHOLDS = {
	/** An interval further than this from the median interval is uneven. */
	unevenIntervalToleranceSec: 0.003,
	/** Largest distance a beat may sit from a steady region's line. */
	regionToleranceSec: 0.025,
	/** Fewest beat intervals a steady region needs (four bars). */
	minRegionBeats: 16,
	/** Share of the grid's time the merged steady span must cover. */
	steadyMinCoverage: 0.5,
	/** Share of ALL beats that must sit on the steady line. */
	steadyMinOnLine: 0.7
} as const;

export type GridQualityThresholds = { readonly [K in keyof typeof GRID_QUALITY_THRESHOLDS]: number };

export interface GridQuality {
	gridClass: GridClass;
	/** Why the class is `unknown`; null for every other class. */
	reason: string | null;
	intervalCount: number;
	/** Intervals more than the tolerance from the median interval. */
	unevenIntervalCount: number;
	/** Largest interval deviation from the median, in ms (0 when none). */
	worstDeviationMs: number;
	/** Track time of the worst interval's first beat. */
	worstAtSec: number;
	/** 60 / median interval, or null without an interval. */
	medianBpm: number | null;
	/** Beats whose stored BPM differs from the beat before; null when the
	 * grid carries no per-beat BPM. */
	tempoMarkerCount: number | null;
	/** Share of the grid's time the merged steady span covers (flagged only). */
	steadyCoverage: number | null;
	/** Share of all beats within the region tolerance of the steady line. */
	steadyOnLine: number | null;
	/** BPM of that steady line. */
	steadyLineBpm: number | null;
}

export const REASON_NO_GRID_DATA = 'no_grid_data';

/** How every flagged message ends: sync may wander, the track is not broken. */
export const GRID_FLAG_CONSEQUENCE = 'Beat Sync may wander on this track';

const UNKNOWN_REASON_TEXT: Readonly<Record<string, string>> = {
	no_grid_data: 'no beatgrid is stored for this track',
	not_scanned: 'this track has not been scanned yet',
	anlz_missing: 'the rekordbox analysis file is missing',
	anlz_unreadable: 'the rekordbox analysis file could not be read',
	no_rekordbox_grid: 'this track has no rekordbox analysis'
};

/** BPM steps tried coarsest first (`grid_fit.ROUND_STEPS`). */
const ROUND_STEPS = [1.0, 0.5, 0.1, 0.01] as const;

// ----------------------------------------------------------------- helpers

/** Python's `round()`: ties go to the even neighbor. The constant-region
 * recipe is a port, so it rounds the way its reference does. */
function _roundHalfEven(value: number): number {
	const floor = Math.floor(value);
	const diff = value - floor;
	if (diff < 0.5) return floor;
	else if (diff > 0.5) return floor + 1;
	return floor % 2 === 0 ? floor : floor + 1;
}

function _roundHalfUp(value: number): number {
	return Math.floor(value + 0.5);
}

/** Fixed decimals with half-up rounding, the same arithmetic as `_fixed` in
 * grid_quality.py, so both languages print the same digits. */
function _fixed(value: number, digits: number): string {
	const scale = 10 ** digits;
	return (_roundHalfUp(value * scale) / scale).toFixed(digits);
}

function _median(values: readonly number[]): number {
	const ordered = [...values].sort((left, right) => left - right);
	const mid = ordered.length >> 1;
	return ordered.length % 2 === 1 ? ordered[mid] : (ordered[mid - 1] + ordered[mid]) / 2;
}

function _unknown(reason: string): GridQuality {
	return {
		gridClass: 'unknown',
		reason,
		intervalCount: 0,
		unevenIntervalCount: 0,
		worstDeviationMs: 0,
		worstAtSec: 0,
		medianBpm: null,
		tempoMarkerCount: null,
		steadyCoverage: null,
		steadyOnLine: null,
		steadyLineBpm: null
	};
}

// ---------------------------------------------- constant-region recipe
// A port of `apps/analysis_beatgrid/const_regions.py` (find_const_regions,
// _longest, _extend, _try_merge, _line_for_span) and `grid_fit.round_bpm`.

interface ConstRegion {
	lo: number;
	hi: number;
	startSec: number;
	endSec: number;
}

interface Span {
	startSec: number;
	endSec: number;
	beatCount: number;
	members: ConstRegion[];
}

function _regionBeats(region: ConstRegion): number {
	return region.hi - region.lo;
}

/** `[t_lo, period]` of the line through the region's first and last beats,
 * when every beat in `lo..hi` is within `toleranceSec` of it. */
function _fits(times: readonly number[], lo: number, hi: number, toleranceSec: number): [number, number] | null {
	const start = times[lo];
	const period = (times[hi] - times[lo]) / (hi - lo);
	if (!(period > 0)) return null;
	for (let index = lo; index <= hi; index++) {
		if (Math.abs(times[index] - (start + (index - lo) * period)) > toleranceSec) return null;
	}
	return [start, period];
}

function _findConstRegions(times: readonly number[], toleranceSec: number): ConstRegion[] {
	const regions: ConstRegion[] = [];
	let lo = 0;
	while (lo < times.length - 1) {
		let hi = lo + 1;
		let line = _fits(times, lo, hi, toleranceSec);
		if (line === null) {
			regions.push({ lo, hi, startSec: times[lo], endSec: times[hi] });
			lo = hi;
			continue;
		}
		while (hi + 1 < times.length) {
			const wider = _fits(times, lo, hi + 1, toleranceSec);
			if (wider === null) break;
			hi += 1;
			line = wider;
		}
		regions.push({ lo, hi, startSec: line[0], endSec: line[0] + (hi - lo) * line[1] });
		lo = hi;
	}
	return regions;
}

function _longestRegion(regions: readonly ConstRegion[], minBeats: number): number | null {
	let best: number | null = null;
	for (let index = 0; index < regions.length; index++) {
		const region = regions[index];
		if (_regionBeats(region) < minBeats) continue;
		if (best === null || region.endSec - region.startSec > regions[best].endSec - regions[best].startSec) {
			best = index;
		}
	}
	return best;
}

function _tryMerge(span: Span, region: ConstRegion, before: boolean, toleranceSec: number): Span | null {
	const period = (span.endSec - span.startSec) / span.beatCount;
	const gapSec = before ? span.endSec - region.startSec : region.endSec - span.startSec;
	const beatCount = _roundHalfEven(gapSec / period);
	if (beatCount <= span.beatCount) return null;
	const start = before ? region.startSec : span.startSec;
	const end = before ? span.endSec : region.endSec;
	const newPeriod = (end - start) / beatCount;
	for (const member of [...span.members, region]) {
		for (const time of [member.startSec, member.endSec]) {
			const tick = _roundHalfEven((time - start) / newPeriod);
			if (Math.abs(time - (start + tick * newPeriod)) > toleranceSec) return null;
		}
	}
	const regionPeriod = (region.endSec - region.startSec) / _regionBeats(region);
	if (Math.abs(regionPeriod - newPeriod) * _regionBeats(region) > toleranceSec) return null;
	return {
		startSec: start,
		endSec: end,
		beatCount,
		members: before ? [region, ...span.members] : [...span.members, region]
	};
}

function _extend(regions: readonly ConstRegion[], reference: number, toleranceSec: number): Span {
	const ref = regions[reference];
	let span: Span = { startSec: ref.startSec, endSec: ref.endSec, beatCount: _regionBeats(ref), members: [ref] };
	for (let index = reference - 1; index >= 0; index--) {
		span = _tryMerge(span, regions[index], true, toleranceSec) ?? span;
	}
	for (let index = reference + 1; index < regions.length; index++) {
		span = _tryMerge(span, regions[index], false, toleranceSec) ?? span;
	}
	return span;
}

/** Coarsest BPM step whose line drifts at most `toleranceSec` over half the span. */
function _roundBpm(bpm: number, beatSpan: number, toleranceSec: number): number {
	const period = 60 / bpm;
	const half = Math.max(beatSpan, 1) / 2;
	for (const step of ROUND_STEPS) {
		const candidate = _roundHalfEven(bpm / step) * step;
		if (candidate <= 0) continue;
		if (Math.abs(60 / candidate - period) * half <= toleranceSec) return Number(candidate.toFixed(2));
	}
	return Number(bpm.toFixed(2));
}

/** `{coverage, onLine, lineBpm}` of the one steady line, or null when no
 * region of `minRegionBeats` exists. */
function _steadyLine(
	times: readonly number[],
	thresholds: GridQualityThresholds
): { coverage: number; onLine: number; lineBpm: number } | null {
	const toleranceSec = thresholds.regionToleranceSec;
	const regions = _findConstRegions(times, toleranceSec);
	const reference = _longestRegion(regions, thresholds.minRegionBeats);
	if (reference === null) return null;
	const span = _extend(regions, reference, toleranceSec);
	const lineBpm = _roundBpm(60 / ((span.endSec - span.startSec) / span.beatCount), span.beatCount, toleranceSec);
	const period = 60 / lineBpm;
	// Phase: the mean of every member beat's offset from the rounded grid.
	const memberBeats = new Set<number>();
	for (const member of span.members) {
		for (let index = member.lo; index <= member.hi; index++) memberBeats.add(index);
	}
	const ordered = [...memberBeats].sort((left, right) => left - right);
	let residualSum = 0;
	for (const index of ordered) {
		residualSum += times[index] - _roundHalfEven((times[index] - span.startSec) / period) * period;
	}
	const anchor = residualSum / ordered.length;
	let onLineCount = 0;
	for (const time of times) {
		const tick = _roundHalfUp((time - anchor) / period);
		if (Math.abs(time - (anchor + tick * period)) <= toleranceSec) onLineCount += 1;
	}
	return {
		coverage: (span.endSec - span.startSec) / (times[times.length - 1] - times[0]),
		onLine: onLineCount / times.length,
		lineBpm
	};
}

// -------------------------------------------------------------------- rule

/**
 * Classify one beatgrid from its beat times (seconds, in grid order).
 * `beatBpms` is the per-beat stored BPM; it only feeds `tempoMarkerCount`.
 */
export function classifyGrid(
	beatTimesSec: readonly number[],
	beatBpms: readonly number[] | null = null,
	thresholds: GridQualityThresholds = GRID_QUALITY_THRESHOLDS
): GridQuality {
	if (beatTimesSec.length < 2) return _unknown(REASON_NO_GRID_DATA);
	if (beatBpms !== null && beatBpms.length !== beatTimesSec.length) {
		throw new RangeError(`beatBpms has ${beatBpms.length} entries for ${beatTimesSec.length} beats`);
	}
	const intervalCount = beatTimesSec.length - 1;
	const intervals: number[] = new Array(intervalCount);
	for (let index = 0; index < intervalCount; index++) {
		intervals[index] = beatTimesSec[index + 1] - beatTimesSec[index];
	}
	const medianSec = _median(intervals);
	let unevenIntervalCount = 0;
	let worstDeviationSec = 0;
	let worstAtSec = beatTimesSec[0];
	for (let index = 0; index < intervalCount; index++) {
		const deviationSec = Math.abs(intervals[index] - medianSec);
		if (deviationSec <= thresholds.unevenIntervalToleranceSec) continue;
		unevenIntervalCount += 1;
		if (deviationSec > worstDeviationSec) {
			worstDeviationSec = deviationSec;
			worstAtSec = beatTimesSec[index];
		}
	}
	let tempoMarkerCount: number | null = null;
	if (beatBpms !== null) {
		tempoMarkerCount = 0;
		for (let index = 1; index < beatBpms.length; index++) {
			if (beatBpms[index] !== beatBpms[index - 1]) tempoMarkerCount += 1;
		}
	}
	const base = {
		reason: null,
		intervalCount,
		unevenIntervalCount,
		worstDeviationMs: worstDeviationSec * 1000,
		worstAtSec,
		medianBpm: medianSec > 0 ? 60 / medianSec : null,
		tempoMarkerCount
	};
	if (unevenIntervalCount === 0) {
		return { ...base, gridClass: 'ok', steadyCoverage: null, steadyOnLine: null, steadyLineBpm: null };
	}
	const steady = _steadyLine(beatTimesSec, thresholds);
	const isSteady =
		steady !== null &&
		steady.coverage >= thresholds.steadyMinCoverage &&
		steady.onLine >= thresholds.steadyMinOnLine;
	return {
		...base,
		gridClass: isSteady ? 'suspect' : 'variable_tempo',
		steadyCoverage: steady?.coverage ?? null,
		steadyOnLine: steady?.onLine ?? null,
		steadyLineBpm: steady?.lineBpm ?? null
	};
}

// ----------------------------------------------------------------- wording

/** The clause the deck badge has always shown for an uneven grid. */
export function unevenIntervalsPhrase(quality: GridQuality): string {
	return (
		`${quality.unevenIntervalCount} of ${quality.intervalCount} beat intervals ` +
		`are uneven (worst ${_fixed(quality.worstDeviationMs, 0)} ms off at ` +
		`${_fixed(quality.worstAtSec, 1)} s)`
	);
}

/** What is uneven about a FLAGGED grid, without prefix or consequence. */
export function gridFlagClause(quality: GridQuality): string {
	if (quality.gridClass === 'suspect') {
		if (quality.steadyOnLine === null || quality.steadyLineBpm === null) {
			throw new Error('a suspect grid always carries its steady line');
		}
		return (
			`${unevenIntervalsPhrase(quality)}, but ` +
			`${_fixed(quality.steadyOnLine * 100, 0)}% of beats sit on one ` +
			`${_fixed(quality.steadyLineBpm, 2)} BPM line (the grid is uneven, ` +
			'the music is not)'
		);
	} else if (quality.gridClass === 'variable_tempo') {
		const markers = quality.tempoMarkerCount;
		const markerText = markers === null ? '' : `, ${markers} tempo marker${markers === 1 ? '' : 's'}`;
		return `tempo changes through this track: ${unevenIntervalsPhrase(quality)}${markerText}`;
	}
	throw new Error(`not a flagged grid class: ${quality.gridClass}`);
}

/** One line for the hover title: what was measured, with the numbers. */
export function gridQualityMessage(
	quality: GridQuality,
	thresholds: GridQualityThresholds = GRID_QUALITY_THRESHOLDS
): string {
	if (quality.gridClass === 'unknown') {
		if (quality.reason === null) throw new Error('an unknown grid always says why');
		const text = UNKNOWN_REASON_TEXT[quality.reason];
		if (text === undefined) throw new Error(`unhandled unknown-grid reason: ${quality.reason}`);
		return `Beatgrid not checked: ${text}`;
	} else if (quality.gridClass === 'ok') {
		return (
			`Beatgrid: all ${quality.intervalCount} beat intervals are within ` +
			`${_fixed(thresholds.unevenIntervalToleranceSec * 1000, 0)} ms of the ` +
			'median (evenly spaced; not checked against the audio)'
		);
	} else if (quality.gridClass === 'suspect' || quality.gridClass === 'variable_tempo') {
		return `Beatgrid: ${gridFlagClause(quality)} - ${GRID_FLAG_CONSEQUENCE}`;
	}
	const unhandled: never = quality.gridClass;
	throw new Error(`Unhandled: ${unhandled}`);
}
