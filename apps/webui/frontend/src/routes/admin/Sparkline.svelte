<script lang="ts">
	/**
	 * One KPI's readings across snapshots.
	 *
	 * Nulls are never plotted as zero: a not-measured snapshot draws a hollow
	 * dashed tick on the midline, and any segment spanning one is dashed, so a
	 * gap can never read as continuous data. The final segment and end dot take
	 * the good/bad colour so direction survives without relying on colour alone.
	 */
	import { formatDeltaWithUnit, formatWithUnit, judge, type Verdict } from './format';
	import { SPARK_H, SPARK_W, sparkGeometry, sparkX, sparkY } from './spark-geometry';
	import { tip } from './tooltip.svelte';
	import type { KpiDef, KpiSnapshot } from './kpi-api';

	interface Props {
		metric: string;
		kpi: KpiDef;
		snapshots: KpiSnapshot[];
		verdict: Verdict;
	}

	const { metric, kpi, snapshots, verdict }: Props = $props();

	const W = SPARK_W;
	const H = SPARK_H;

	const geometry = $derived(sparkGeometry(metric, snapshots));
	const present = $derived(geometry.present);
	const missing = $derived(geometry.missing);
	const segments = $derived(geometry.segments);

	function xAt(index: number): number {
		return sparkX(index, snapshots.length);
	}

	function yAt(value: number): number {
		return sparkY(value, geometry.bounds);
	}

	const statusColor = $derived(
		verdict === 'good' ? 'var(--kpi-ok)' : verdict === 'bad' ? 'var(--danger)' : 'var(--muted)'
	);

	function dotTip(point: { index: number; value: number }, order: number) {
		const previous = order > 0 ? present[order - 1] : null;
		if (!previous) {
			return {
				title: snapshots[point.index].label,
				subtitle: formatWithUnit(point.value, kpi.unit),
				body: ['First reading for this KPI.']
			};
		}
		const delta = point.value - previous.value;
		const tone = judge(delta, kpi.direction);
		const word = delta === 0 ? 'unchanged' : tone === 'good' ? 'improved' : 'regressed';
		return {
			title: snapshots[point.index].label,
			subtitle: formatWithUnit(point.value, kpi.unit),
			lines: [
				{
					text: `${formatDeltaWithUnit(delta, kpi.unit)} vs ${snapshots[previous.index].label} (${word})`,
					tone
				}
			]
		};
	}
</script>

<svg class="spark" viewBox="0 0 {W} {H}" preserveAspectRatio="none" aria-hidden="true">
	{#each segments as seg}
		<line
			x1={seg.x1}
			y1={seg.y1}
			x2={seg.x2}
			y2={seg.y2}
			stroke={seg.last ? statusColor : 'var(--muted)'}
			stroke-width="2"
			stroke-linecap="round"
			vector-effect="non-scaling-stroke"
			stroke-dasharray={seg.gapped ? '3,3' : undefined}
		/>
	{/each}

	{#each missing as gapIndex}
		<circle
			cx={xAt(gapIndex)}
			cy={geometry.midlineY}
			r="2.5"
			fill="none"
			stroke="var(--muted)"
			stroke-width="1.25"
			stroke-dasharray="1.5,1.5"
			use:tip={{
				title: snapshots[gapIndex].label,
				body: ['Not measured in this run - different from a real 0.']
			}}
		>
			<title>{snapshots[gapIndex].label}: not measured in this run.</title>
		</circle>
	{/each}

	{#each present as point, order}
		<circle
			cx={xAt(point.index)}
			cy={yAt(point.value)}
			r={order === present.length - 1 ? 4 : 2.5}
			fill={order === present.length - 1 ? statusColor : 'var(--muted)'}
			stroke="var(--surface)"
			stroke-width="2"
			use:tip={dotTip(point, order)}
		>
			<title
				>{snapshots[point.index].label}: {formatWithUnit(point.value, kpi.unit)}</title
			>
		</circle>
	{/each}
</svg>

<style>
	.spark {
		width: 100%;
		height: 44px;
		display: block;
		overflow: visible;
	}
	/* The hollow not-measured ticks are fill="none", so the default
	 * visiblePainted hit-testing would only catch their 1.25px stroke and the
	 * "not measured, not a real 0" explainer would be unreachable. */
	.spark circle {
		pointer-events: all;
	}
</style>
