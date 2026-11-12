<script lang="ts">
	// ANALYSIS-11723: "Re-analyze grid" with a BPM range picker, inside the
	// analysis dots popover. The logic is reanalyze-grid.ts (unit tested).
	import type { AnalysisOrder } from '$lib/rb/api-ingest';
	import { BPM_RANGES, GLOBAL_RANGE, reanalyzeGrid } from '$lib/rb/reanalyze-grid';

	let {
		stableId,
		upsertJob,
		toast
	}: {
		stableId: string;
		upsertJob: (phase: AnalysisOrder['phase']) => void;
		toast: (message: string, kind: 'info' | 'error') => void;
	} = $props();

	let range = $state('');
	let running = $state(false);

	async function run(): Promise<void> {
		running = true;
		try {
			await reanalyzeGrid(stableId, range === '' ? null : range, { upsertJob, toast });
		} finally {
			running = false;
		}
	}
</script>

<div class="reanalyze" data-testid="reanalyze-grid">
	<select bind:value={range} disabled={running} aria-label="BPM range for this track">
		<option value="">Global ({GLOBAL_RANGE})</option>
		{#each BPM_RANGES.filter((r) => r !== GLOBAL_RANGE) as choice (choice)}
			<option value={choice}>{choice} BPM</option>
		{/each}
	</select>
	<button
		type="button"
		disabled={running}
		title="Re-runs our beatgrid for this track inside the BPM range you pick"
		onclick={() => void run()}
	>
		{running ? 'Re-analyzing...' : 'Re-analyze grid'}
	</button>
</div>

<style>
	.reanalyze {
		display: flex;
		gap: 4px;
		align-items: center;
		padding: 4px 6px;
		border-top: 1px solid var(--rb-border, #333);
	}
	.reanalyze select,
	.reanalyze button {
		font: inherit;
		font-size: 11px;
	}
</style>
