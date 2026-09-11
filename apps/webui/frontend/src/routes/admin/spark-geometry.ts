/**
 * Sparkline geometry for one KPI across snapshots.
 *
 * M1: unmeasured is not zero. A snapshot that carried `null` for this metric is
 * a GAP - a hollow dashed tick on the midline, with the segment spanning it
 * dashed - never a point plotted at the axis floor, which would read as a
 * collapse to zero. A genuine `0` is an ordinary plotted value.
 *
 * PERF-DASH-02: perf cards opt into calendar-date x via SparkAxis mode 'date';
 * farm cards keep index-based spacing (default mode 'index').
 *
 * Lives outside Sparkline.svelte so the rule is unit-testable: the server-side
 * sibling of this rule IS covered (test_api_reports_unmeasured_rather_than_a_number)
 * which is precisely what made the frontend gap easy to miss.
 */

import type { KpiSnapshot } from './kpi-api';

export const SPARK_W = 180;
export const SPARK_H = 44;
export const SPARK_PAD = 6;

export interface SparkAxis {
	mode?: 'index' | 'date';
	today?: string;
}

export interface SparkPoint {
	index: number;
	value: number;
	x: number;
}

export interface SparkSegment {
	x1: number;
	y1: number;
	x2: number;
	y2: number;
	/** The most recent segment takes the good/bad colour. */
	last: boolean;
	/** True when this segment jumps over at least one unmeasured snapshot. */
	gapped: boolean;
}

export interface SparkGeometry {
	/** Snapshots with a real number, in order. */
	present: SparkPoint[];
	/** Snapshot indices that measured nothing - drawn as hollow dashed ticks. */
	missing: number[];
	segments: SparkSegment[];
	bounds: { lo: number; hi: number };
	/** y for the midline the not-measured ticks sit on. */
	midlineY: number;
	/** x for every snapshot index; geometry is the single source of truth. */
	xs: number[];
}

const DAY_MS = 24 * 60 * 60 * 1000;

export function snapshotDay(ts: string): string {
	const match = /^(\d{4}-\d{2}-\d{2})/.exec(ts);
	if (!match) throw new Error(`ts does not start with YYYY-MM-DD: ${ts}`);
	return match[1];
}

export function utcToday(now?: Date): string {
	return (now ?? new Date()).toISOString().slice(0, 10);
}

export function calendarDays(fromDay: string, toDay: string): number {
	const from = Date.parse(`${fromDay}T00:00:00Z`);
	const to = Date.parse(`${toDay}T00:00:00Z`);
	return Math.round((to - from) / DAY_MS);
}

function indexXs(snapshotCount: number): number[] {
	return Array.from({ length: snapshotCount }, (_, index) => sparkX(index, snapshotCount));
}

function dateXs(snapshots: KpiSnapshot[], today: string): number[] {
	const startDay = snapshotDay(snapshots[0].ts);
	const windowDays = calendarDays(startDay, today);
	if (windowDays === 0) return indexXs(snapshots.length);

	const dayCounts = new Map<string, number>();
	for (const snapshot of snapshots) {
		const day = snapshotDay(snapshot.ts);
		dayCounts.set(day, (dayCounts.get(day) ?? 0) + 1);
	}
	const dayRanks = new Map<string, number>();
	const span = SPARK_W - 2 * SPARK_PAD;

	return snapshots.map((snapshot) => {
		const day = snapshotDay(snapshot.ts);
		const rank = dayRanks.get(day) ?? 0;
		dayRanks.set(day, rank + 1);
		const count = dayCounts.get(day)!;
		const dayOffset = calendarDays(startDay, day) + rank / count;
		return SPARK_PAD + span * (dayOffset / windowDays);
	});
}

function computeXs(snapshots: KpiSnapshot[], axis?: SparkAxis): number[] {
	if (snapshots.length === 0) return [];
	const mode = axis?.mode ?? 'index';
	if (mode === 'index') return indexXs(snapshots.length);
	return dateXs(snapshots, axis?.today ?? utcToday());
}

export function sparkGeometry(
	metric: string,
	snapshots: KpiSnapshot[],
	axis?: SparkAxis
): SparkGeometry {
	const raw = snapshots.map((snapshot) => snapshot.values[metric] ?? null);
	const xs = computeXs(snapshots, axis);
	const present: SparkPoint[] = [];
	const missing: number[] = [];
	raw.forEach((value, index) => {
		// typeof, NOT truthiness: a real 0 is a measurement and must plot.
		if (typeof value === 'number') present.push({ index, value, x: xs[index] });
		else missing.push(index);
	});

	const values = present.map((point) => point.value);
	let lo = values.length > 0 ? Math.min(...values) : 0;
	let hi = values.length > 0 ? Math.max(...values) : 0;
	if (lo === hi) {
		lo -= 1;
		hi += 1;
	}

	const yAt = (value: number): number =>
		SPARK_H - SPARK_PAD - (SPARK_H - 2 * SPARK_PAD) * ((value - lo) / (hi - lo));

	const segments = present.slice(0, -1).map((from, i) => {
		const to = present[i + 1];
		return {
			x1: from.x,
			y1: yAt(from.value),
			x2: to.x,
			y2: yAt(to.value),
			last: i === present.length - 2,
			gapped: to.index - from.index > 1
		};
	});

	return { present, missing, segments, bounds: { lo, hi }, midlineY: SPARK_H / 2, xs };
}

/** x for a snapshot index, exported so the component and the tests agree. */
export function sparkX(index: number, snapshotCount: number): number {
	if (snapshotCount <= 1) return SPARK_W / 2;
	return SPARK_PAD + ((SPARK_W - 2 * SPARK_PAD) * index) / (snapshotCount - 1);
}

/** y for a value under the given bounds. */
export function sparkY(value: number, bounds: { lo: number; hi: number }): number {
	return (
		SPARK_H - SPARK_PAD - (SPARK_H - 2 * SPARK_PAD) * ((value - bounds.lo) / (bounds.hi - bounds.lo))
	);
}
