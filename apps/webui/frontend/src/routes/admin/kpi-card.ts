/**
 * The reading model behind one KPI card: which snapshots actually measured this
 * metric, the latest and previous of those, the direction-aware verdict, the
 * provenance badge, and the consolidated hover explainer.
 *
 * Lives outside KpiTile.svelte so the H4 rule - a hand-typed or config-constant
 * reading must never render as a measurement - can be asserted directly. A
 * component is not importable by the node --test suite, and this is exactly the
 * kind of rule a refactor flattens without noticing.
 */

import {
	ARROW,
	formatDeltaWithUnit,
	formatWithUnit,
	judge,
	type Verdict
} from './format';
import type { KpiDef, KpiSnapshot } from './kpi-api';
import { KPI_WHY } from './kpi-why';
import { originBadge, originText } from './kpi-provenance';
import type { TipContent } from './tooltip.svelte';

/** One snapshot that carried a real number for this metric. */
export interface KpiPoint {
	/** Index into the snapshots array, so the run label and ts stay reachable. */
	index: number;
	value: number;
}

export interface KpiCardModel {
	points: KpiPoint[];
	latest: KpiPoint | null;
	previous: KpiPoint | null;
	delta: number;
	verdict: Verdict;
	/** 'typed' | 'config' | '' - empty means derived, which is deliberately unbadged. */
	badge: string;
	tip: TipContent;
}

/** Snapshots where this metric is a real number. Nulls are gaps, never zeros. */
export function measuredPoints(metric: string, snapshots: KpiSnapshot[]): KpiPoint[] {
	return snapshots
		.map((snapshot, index) => ({ index, value: snapshot.values[metric] ?? null }))
		.filter((point): point is KpiPoint => typeof point.value === 'number');
}

export function buildKpiCard(
	metric: string,
	kpi: KpiDef,
	snapshots: KpiSnapshot[]
): KpiCardModel {
	const directionText = kpi.direction === 'higher_better' ? 'Higher is better.' : 'Lower is better.';
	const why = KPI_WHY[metric] ?? '';
	const points = measuredPoints(metric, snapshots);
	const latest = points.length > 0 ? points[points.length - 1] : null;
	const previous = points.length >= 2 ? points[points.length - 2] : null;
	const delta = latest !== null && previous !== null ? latest.value - previous.value : 0;
	const verdict: Verdict = previous !== null ? judge(delta, kpi.direction) : 'flat';

	const body = [kpi.title];
	if (why) body.push(why);
	if (latest === null) {
		body.push(`Not measured in any of the ${snapshots.length} snapshots yet.`);
		return {
			points,
			latest,
			previous,
			delta,
			verdict,
			badge: '',
			tip: { title: kpi.label, subtitle: `${kpi.unit} - ${directionText}`, body }
		};
	}

	const origin = snapshots[latest.index].provenance[metric];
	body.push(`Latest reading from ${snapshots[latest.index].label} (${snapshots[latest.index].ts}).`);
	const originLine = originText(origin);
	if (originLine !== null) body.push(originLine);

	const lines =
		previous !== null
			? [
					{
						text: `${ARROW[verdict]} ${formatDeltaWithUnit(delta, kpi.unit)} since ${snapshots[previous.index].label}`,
						tone: verdict
					}
				]
			: [{ text: 'First reading; nothing to compare against yet.', tone: 'flat' as const }];

	return {
		points,
		latest,
		previous,
		delta,
		verdict,
		badge: originBadge(origin),
		tip: {
			title: kpi.label,
			subtitle: `${formatWithUnit(latest.value, kpi.unit)} - ${directionText}`,
			lines,
			body
		}
	};
}
