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
	import { ARROW, formatDeltaWithUnit, formatValue, isDuration } from './format';
	import { buildKpiCard } from './kpi-card';
	import { originText } from './kpi-provenance';
	import { tip } from './tooltip.svelte';
	import type { KpiDef, KpiSnapshot } from './kpi-api';

	interface Props {
		metric: string;
		kpi: KpiDef;
		snapshots: KpiSnapshot[];
	}

	const { metric, kpi, snapshots }: Props = $props();

	const card = $derived(buildKpiCard(metric, kpi, snapshots));
	const points = $derived(card.points);
	const latest = $derived(card.latest);
	const previous = $derived(card.previous);
	const delta = $derived(card.delta);
	const verdict = $derived(card.verdict);
	const badge = $derived(card.badge);
	const cardTip = $derived(card.tip);
</script>

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<div class="tile" tabindex="0" use:tip={cardTip}>
	<div class="tile-label">
		{kpi.label}
		{#if latest && badge}
			<span class="origin" title={originText(snapshots[latest.index].provenance[metric]) ?? ''}
				>{badge}</span
			>
		{/if}
	</div>

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
	.origin {
		font-size: 0.62rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		border: 1px solid var(--border);
		border-radius: 3px;
		padding: 0 0.25rem;
		color: var(--muted);
		white-space: nowrap;
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
