/**
 * Sparkline geometry for one KPI across snapshots.
 *
 * M1: unmeasured is not zero. A snapshot that carried `null` for this metric is
 * a GAP - a hollow dashed tick on the midline, with the segment spanning it
 * dashed - never a point plotted at the axis floor, which would read as a
 * collapse to zero. A genuine `0` is an ordinary plotted value.
 *
 * Lives outside Sparkline.svelte so the rule is unit-testable: the server-side
 * sibling of this rule IS covered (test_api_reports_unmeasured_rather_than_a_number)
 * which is precisely what made the frontend gap easy to miss.
 */

import type { KpiSnapshot } from './kpi-api';

export const SPARK_W = 180;
export const SPARK_H = 44;
export const SPARK_PAD = 6;

export interface SparkPoint {
	index: number;
	value: number;
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
}

export function sparkGeometry(metric: string, snapshots: KpiSnapshot[]): SparkGeometry {
	const raw = snapshots.map((snapshot) => snapshot.values[metric] ?? null);
	const present: SparkPoint[] = [];
	const missing: number[] = [];
	raw.forEach((value, index) => {
		// typeof, NOT truthiness: a real 0 is a measurement and must plot.
		if (typeof value === 'number') present.push({ index, value });
		else missing.push(index);
	});

	const values = present.map((point) => point.value);
	let lo = values.length > 0 ? Math.min(...values) : 0;
	let hi = values.length > 0 ? Math.max(...values) : 0;
	if (lo === hi) {
		lo -= 1;
		hi += 1;
	}

	const xAt = (index: number): number =>
		snapshots.length <= 1
			? SPARK_W / 2
			: SPARK_PAD + ((SPARK_W - 2 * SPARK_PAD) * index) / (snapshots.length - 1);
	const yAt = (value: number): number =>
		SPARK_H - SPARK_PAD - (SPARK_H - 2 * SPARK_PAD) * ((value - lo) / (hi - lo));

	const segments = present.slice(0, -1).map((from, i) => {
		const to = present[i + 1];
		return {
			x1: xAt(from.index),
			y1: yAt(from.value),
			x2: xAt(to.index),
			y2: yAt(to.value),
			last: i === present.length - 2,
			gapped: to.index - from.index > 1
		};
	});

	return { present, missing, segments, bounds: { lo, hi }, midlineY: SPARK_H / 2 };
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
