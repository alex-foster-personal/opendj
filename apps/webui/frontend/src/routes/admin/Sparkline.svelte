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
	import { tip } from './tooltip.svelte';
	import type { KpiDef, KpiSnapshot } from './kpi-api';

	interface Props {
		metric: string;
		kpi: KpiDef;
		snapshots: KpiSnapshot[];
		verdict: Verdict;
	}

	const { metric, kpi, snapshots, verdict }: Props = $props();

	const W = 180;
	const H = 44;
	const PAD = 6;

	const raw = $derived(snapshots.map((s) => s.values[metric] ?? null));
	const present = $derived(
		raw
			.map((value, index) => ({ index, value }))
			.filter((point): point is { index: number; value: number } => typeof point.value === 'number')
	);
	const missing = $derived(
		raw.map((value, index) => ({ index, value })).filter((point) => point.value === null)
	);

	const bounds = $derived.by(() => {
		const values = present.map((p) => p.value);
		let lo = Math.min(...values);
		let hi = Math.max(...values);
		if (lo === hi) {
			lo -= 1;
			hi += 1;
		}
		return { lo, hi };
	});

	function xAt(index: number): number {
		if (snapshots.length <= 1) return W / 2;
		return PAD + ((W - 2 * PAD) * index) / (snapshots.length - 1);
	}

	function yAt(value: number): number {
		return H - PAD - (H - 2 * PAD) * ((value - bounds.lo) / (bounds.hi - bounds.lo));
	}

	const statusColor = $derived(
		verdict === 'good' ? 'var(--kpi-ok)' : verdict === 'bad' ? 'var(--danger)' : 'var(--muted)'
	);

	const segments = $derived(
		present.slice(0, -1).map((from, i) => {
			const to = present[i + 1];
			return {
				x1: xAt(from.index),
				y1: yAt(from.value),
				x2: xAt(to.index),
				y2: yAt(to.value),
				last: i === present.length - 2,
				gapped: to.index - from.index > 1
			};
		})
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

	{#each missing as gap}
		<circle
			cx={xAt(gap.index)}
			cy={H / 2}
			r="2.5"
			fill="none"
			stroke="var(--muted)"
			stroke-width="1.25"
			stroke-dasharray="1.5,1.5"
			use:tip={{
				title: snapshots[gap.index].label,
				body: ['Not measured in this run - different from a real 0.']
			}}
		>
			<title>{snapshots[gap.index].label}: not measured in this run.</title>
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
