<script lang="ts">
	/**
	 * One KPI card: latest reading, direction-aware delta, sparkline.
	 *
	 * The WHOLE card is the hover target - pointing anywhere on it raises the
	 * explainer (what the number is, which way is better, why it matters, where
	 * the latest reading came from). Sparkline dots override it with their own
	 * per-run readout while pointed at, then it comes back.
	 */
	import Sparkline from './Sparkline.svelte';
	import {
		ARROW,
		formatDeltaWithUnit,
		formatValue,
		formatWithUnit,
		isDuration,
		judge,
		type Verdict
	} from './format';
	import { KPI_WHY } from './kpi-why';
	import { tip, type TipContent } from './tooltip.svelte';
	import type { KpiDef, KpiSnapshot } from './kpi-api';

	interface Props {
		metric: string;
		kpi: KpiDef;
		snapshots: KpiSnapshot[];
	}

	const { metric, kpi, snapshots }: Props = $props();

	const directionText = $derived(
		kpi.direction === 'higher_better' ? 'Higher is better.' : 'Lower is better.'
	);
	const why = $derived(KPI_WHY[metric] ?? '');

	const points = $derived(
		snapshots
			.map((snapshot, index) => ({ index, value: snapshot.values[metric] ?? null }))
			.filter((point): point is { index: number; value: number } => typeof point.value === 'number')
	);

	const latest = $derived(points.length > 0 ? points[points.length - 1] : null);
	const previous = $derived(points.length >= 2 ? points[points.length - 2] : null);
	const delta = $derived(latest && previous ? latest.value - previous.value : 0);
	const verdict = $derived<Verdict>(previous ? judge(delta, kpi.direction) : 'flat');

	/** One consolidated explainer for the whole card. */
	const cardTip = $derived.by((): TipContent => {
		const body = [kpi.title];
		if (why) body.push(why);
		if (!latest) {
			body.push(`Not measured in any of the ${snapshots.length} snapshots yet.`);
			return {
				title: kpi.label,
				subtitle: `${kpi.unit} - ${directionText}`,
				body
			};
		}
		body.push(`Latest reading from ${snapshots[latest.index].label} (${snapshots[latest.index].ts}).`);
		const lines = previous
			? [
					{
						text: `${ARROW[verdict]} ${formatDeltaWithUnit(delta, kpi.unit)} since ${snapshots[previous.index].label}`,
						tone: verdict
					}
				]
			: [{ text: 'First reading; nothing to compare against yet.', tone: 'flat' as const }];
		return {
			title: kpi.label,
			subtitle: `${formatWithUnit(latest.value, kpi.unit)} - ${directionText}`,
			lines,
			body
		};
	});
</script>

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<div class="tile" tabindex="0" use:tip={cardTip}>
	<div class="tile-label">{kpi.label}</div>

	{#if !latest}
		<div class="value nodata">no data yet</div>
	{:else}
		<div class="value-row">
			<span class="value">{formatValue(latest.value, kpi.unit)}</span>
			{#if !isDuration(kpi.unit)}
				<span class="unit">{kpi.unit}</span>
			{/if}
			<span class="delta {previous ? verdict : 'first'}">
				{#if previous}
					{ARROW[verdict]} {formatDeltaWithUnit(delta, kpi.unit)}
				{:else}
					first reading
				{/if}
			</span>
		</div>
		<Sparkline {metric} {kpi} {snapshots} {verdict} />
		{#if points.length === 1 && snapshots.length > 1}
			<div class="spark-note">single reading</div>
		{/if}
	{/if}
</div>

<style>
	.tile {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 0.75rem 0.85rem;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
		min-width: 0;
		cursor: help;
	}
	.tile:hover,
	.tile:focus-visible {
		border-color: var(--accent-dim);
		background: var(--surface-hover);
		outline: none;
	}
	.tile-label {
		font-size: 0.78rem;
		color: var(--muted);
		line-height: 1.25;
	}
	.value-row {
		display: flex;
		align-items: baseline;
		gap: 0.3rem;
		flex-wrap: wrap;
	}
	.value {
		font-size: 1.55rem;
		font-weight: 600;
		color: var(--fg);
		line-height: 1;
		font-variant-numeric: tabular-nums;
	}
	.value.nodata {
		font-size: 1rem;
		font-weight: 400;
		color: var(--muted);
	}
	.unit {
		font-size: 0.8rem;
		color: var(--muted);
	}
	.delta {
		font-size: 0.78rem;
		font-variant-numeric: tabular-nums;
	}
	.delta.good {
		color: var(--kpi-ok);
	}
	.delta.bad {
		color: var(--danger);
	}
	.delta.flat,
	.delta.first {
		color: var(--muted);
	}
	.spark-note {
		font-size: 0.7rem;
		color: var(--muted);
	}
</style>
