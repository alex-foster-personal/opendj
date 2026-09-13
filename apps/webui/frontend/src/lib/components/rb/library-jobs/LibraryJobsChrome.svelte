<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { ANALYSIS_COLORS, jobProgress } from '$lib/rb/job-progress.svelte';
	import { confirmIngestPending } from '$lib/rb/api-ingest';
	import { ingestPending, refreshIngestPending, startIngestPendingWatch, stopIngestPendingWatch } from '$lib/rb/ingest-pending.svelte';
	import LibraryJobQueuePanel from './LibraryJobQueuePanel.svelte';

	let open = $state(false);
	let rbPopoverOpen = $state(false);
	const ribbon = $derived(jobProgress.ribbon());
	const pendingBatches = $derived(ingestPending.batches);
	const rbLabel = $derived(
		pendingBatches.length > 1
			? `awaiting Rekordbox import (${pendingBatches.length})`
			: 'awaiting Rekordbox import'
	);
	const rbTitle = $derived(
		pendingBatches.map((b) => b.dest_dir).join('\n')
	);

	onMount(() => {
		startIngestPendingWatch();
	});

	onDestroy(() => {
		stopIngestPendingWatch();
	});

	async function confirmBatch(name: string): Promise<void> {
		await confirmIngestPending(name);
		await refreshIngestPending();
		rbPopoverOpen = false;
	}
</script>

{#if ribbon}
	<button
		type="button"
		class="job-ribbon"
		style={`--job:${ANALYSIS_COLORS[ribbon.kind]}; --pct:${Math.max(0.02, ribbon.progress)}`}
		title={`${ribbon.label} offload. Click to open the job queue.`}
		aria-label={ribbon.label}
		onclick={() => (open = !open)}
	>
		<span class="job-ribbon-fill"></span>
		<span class="job-ribbon-label">{ribbon.label}</span>
	</button>
{/if}
{#if pendingBatches.length > 0}
	<div class="rb-pending-wrap">
		<button
			type="button"
			class="rb-pending-chip"
			data-testid="ingest-awaiting-rb"
			title={rbTitle}
			onclick={() => (rbPopoverOpen = !rbPopoverOpen)}
		>
			{rbLabel}
		</button>
		{#if rbPopoverOpen}
			<div class="rb-pending-pop">
				{#each pendingBatches as batch (batch.name)}
					<div class="rb-pending-row">
						<span class="rb-pending-name" title={batch.dest_dir}>{batch.name}</span>
						<button
							type="button"
							class="rb-pending-confirm"
							data-testid="ingest-awaiting-rb-confirm"
							onclick={() => void confirmBatch(batch.name)}
						>
							Rekordbox import done
						</button>
					</div>
				{/each}
			</div>
		{/if}
	</div>
{/if}
<LibraryJobQueuePanel bind:open />

<style>
	.job-ribbon {
		position: relative;
		display: inline-flex;
		align-items: center;
		height: 16px;
		min-width: 72px;
		border: 0;
		background: #222;
		color: inherit;
		overflow: hidden;
		cursor: pointer;
	}
	.job-ribbon-fill {
		position: absolute;
		inset: 0 auto 0 0;
		width: calc(var(--pct, 0.02) * 100%);
		background: var(--job, #888);
		opacity: 0.45;
	}
	.job-ribbon-label { position: relative; padding: 0 6px; font-size: 11px; }
	.rb-pending-wrap {
		position: relative;
		display: inline-flex;
	}
	.rb-pending-chip {
		display: inline-flex;
		align-items: center;
		height: 16px;
		padding: 0 6px;
		border: 1px solid #e8973e;
		background: #2a2218;
		color: #e8973e;
		font-size: 11px;
		cursor: pointer;
	}
	.rb-pending-pop {
		position: absolute;
		top: 100%;
		left: 0;
		z-index: 50;
		min-width: 220px;
		margin-top: 2px;
		padding: 6px;
		background: #1a1f28;
		border: 1px solid #2a2f36;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
	}
	.rb-pending-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		padding: 4px 0;
	}
	.rb-pending-name {
		font-size: 11px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.rb-pending-confirm {
		flex: none;
		padding: 2px 6px;
		font-size: 10px;
		background: #242a31;
		border: 1px solid #2a2f36;
		color: #d7dde3;
		cursor: pointer;
	}
</style>
