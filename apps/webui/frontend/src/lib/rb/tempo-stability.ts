/**
 * Issue #8339: the words the deck strip's tempo lane carries in its hover,
 * and the key a served grid is cached under.
 *
 * The verdict, the segments and every number come from
 * `GET /api/v1/tracks/{id}/tempo-stability`; this module only words them
 * (#7879 veto: the frontend never computes a tempo change or a verdict).
 *
 * Mini-PRD
 *   ✔︎ ✅ 🎯 Every number in the hover says what it is (house rule).
 *     [if] a percent is printed without "vs nominal" and its BPM [then ⛔️] broken
 *   ✔︎ ✅ 🎯 Not analyzed and steady are each worded; the lane says nothing else.
 *     [if] a not-analyzed track hovers as steady [then ⛔️] broken
 *   ✔︎ ✅ 🎯 A grid edit or a source flip refetches: the key moves with the grid.
 *     [if] an edited grid keeps the old key [then ⛔️] broken
 *
 * Pure: the fetch and its cache live in tempo-stability.svelte.ts.
 */

import type { components } from '../api-types';
import type { TempoCurveData } from '../components/rb/wave/tempo-marks';

export type TempoStability = components['schemas']['TempoStabilityOut'];
export type TempoSegment = components['schemas']['TempoSegmentOut'];

export type TempoStabilityView =
	| { state: 'idle' }
	| { state: 'loading' }
	| { state: 'error'; message: string }
	| { state: 'ready'; value: TempoStability };

/** The structural slice of a served beatgrid the cache key reads. */
export interface TempoGridIdentity {
	source: string;
	beats: readonly { t: number }[];
}

/** Moves whenever the served grid does: source flip, new record, grid adjust,
 * or an edit to any interior beat. FNV-1a over every beat time in whole ms, the
 * resolution `/anlz` serves (`t` rounded to 3 dp). */
export function tempoStabilityKey(stableId: string, grid: TempoGridIdentity): string {
	let hash = 0x811c9dc5;
	for (const beat of grid.beats) {
		hash ^= Math.round(beat.t * 1000);
		hash = Math.imul(hash, 0x01000193) >>> 0;
	}
	return `${stableId}|${grid.source}|${grid.beats.length}|${hash.toString(16)}`;
}

/** A failed read is retried no sooner than this after it failed, so a
 * persistent server error cannot become a request loop. */
export const TEMPO_STABILITY_RETRY_MS = 30_000;

/** Whether `entry` (with the time it was stored) should be read again now. */
export function tempoStabilityNeedsFetch(
	entry: (TempoStabilityView & { at?: number }) | undefined,
	nowMs: number
): boolean {
	if (entry === undefined) return true;
	if (entry.state === 'error') return entry.at === undefined || nowMs - entry.at >= TEMPO_STABILITY_RETRY_MS;
	return false;
}

const VERDICT_WORDS: Record<NonNullable<TempoStability['verdict']>, string> = {
	steady: 'steady',
	drift: 'drifts',
	free_intro: 'free-tempo intro, then steady',
	changes: 'changes'
};

const KIND_WORDS: Record<TempoSegment['kind'], string> = {
	steady: 'steady',
	ramp: 'ramp',
	jump: 'held at',
	unstable: 'unstable'
};

function _clock(seconds: number): string {
	const whole = Math.max(0, Math.round(seconds));
	return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`;
}

function _signed(pct: number): string {
	return `${pct >= 0 ? '+' : ''}${pct.toFixed(1)}%`;
}

function _segmentLine(segment: TempoSegment): string {
	const span = `${_clock(segment.start_s)}-${_clock(segment.end_s)}`;
	if (segment.kind === 'ramp') {
		return `${span} ramp ${segment.direction}: ${segment.bpm_start.toFixed(2)} to ${segment.bpm_end.toFixed(2)} BPM`;
	}
	return `${span} ${KIND_WORDS[segment.kind]} ${segment.bpm.toFixed(2)} BPM (${_signed(segment.delta_pct)} vs nominal)`;
}

/** What the tempo curve draws for a read: null until a ready, analyzed answer. */
export function tempoCurveData(view: TempoStabilityView | null): TempoCurveData | null {
	if (view === null || view.state !== 'ready' || view.value.status !== 'ok') return null;
	const { nominal_bpm, largest_change_pct, segments } = view.value;
	if (nominal_bpm === null || largest_change_pct === null) {
		throw new Error('tempo stability: an ok answer always carries nominal_bpm and largest_change_pct');
	}
	return { nominal_bpm, largest_change_pct, segments };
}

/** Most stretches the hover lists before it says how many more there are. */
export const TEMPO_TITLE_MAX_LINES = 6;

/** Hover text for the strip's tempo lane; null while nothing has been read. */
export function tempoStabilityTitle(view: TempoStabilityView): string | null {
	if (view.state === 'idle' || view.state === 'loading') return null;
	if (view.state === 'error') return `Tempo lane: could not read tempo stability (${view.message})`;
	const value = view.value;
	if (value.status === 'not_analyzed') {
		if (value.reason === null) throw new Error('tempo stability: not_analyzed always says why');
		return `Tempo lane: not analyzed - ${value.reason}`;
	}
	if (
		value.verdict === null ||
		value.nominal_bpm === null ||
		value.largest_change_pct === null ||
		value.change_count === null
	) {
		throw new Error('tempo stability: an ok answer always carries a verdict and its numbers');
	}
	const head =
		`Tempo ${VERDICT_WORDS[value.verdict]}: nominal ${value.nominal_bpm.toFixed(2)} BPM, ` +
		`furthest ${value.largest_change_pct.toFixed(1)}% from nominal, ` +
		`${value.change_count} tempo change(s) detected`;
	const moving = value.segments.filter((segment) => segment.kind !== 'steady');
	const lines = moving.slice(0, TEMPO_TITLE_MAX_LINES).map(_segmentLine);
	if (moving.length > TEMPO_TITLE_MAX_LINES) lines.push(`and ${moving.length - TEMPO_TITLE_MAX_LINES} more`);
	return [head, ...lines].join('\n');
}
