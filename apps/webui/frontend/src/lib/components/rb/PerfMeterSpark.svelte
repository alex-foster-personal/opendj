<script lang="ts">
	/**
	 * Presentational sparkline for PerfMeters v2 (PERFMODE-05).
	 * No stores, no timers, no fetch.
	 */
	import { sparkPoints } from '$lib/rb/perf-meter-model';

	interface Props {
		values: Array<number | null>;
		label: string;
		title: string;
	}

	let { values, label, title }: Props = $props();

	const geometry = $derived(sparkPoints(values));
</script>

<div class="perf-spark" {title}>
	<span class="perf-spark-label">{label}</span>
	<svg class="perf-spark-svg" viewBox="0 0 120 32" aria-hidden="true">
		{#each geometry.segments as segment (segment.points)}
			<polyline
				points={segment.points}
				fill="none"
				stroke="currentColor"
				stroke-width="1.5"
				stroke-dasharray={segment.dashed ? '3 2' : undefined}
			/>
		{/each}
	</svg>
</div>

<style>
	.perf-spark {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 120px;
	}
	.perf-spark-label {
		font-size: 9px;
		color: var(--rb-text-dim);
		letter-spacing: 0.02em;
	}
	.perf-spark-svg {
		width: 120px;
		height: 32px;
		color: var(--rb-text-dim);
	}
</style>
