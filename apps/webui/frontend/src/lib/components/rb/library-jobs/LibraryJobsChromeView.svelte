<script lang="ts">
	// Props-only half of LibraryJobsChrome: the bottom-bar job ribbon and the
	// "awaiting Rekordbox import" chip with its confirm popover.
	// LibraryJobsChrome.svelte reads the job and ingest stores, runs the
	// confirm call, and passes the queue panel in as `queuePanel`.
	import type { Snippet } from 'svelte';
	import { ANALYSIS_COLORS, type AnalysisKind } from '$lib/rb/analysis-kinds';

	const {
		ribbon,
		pendingBatches,
		rbPopoverOpen,
		onToggleQueue,
		onToggleRbPopover,
		onConfirmBatch,
		queuePanel
	}: {
		/** The busiest offload job, or null when nothing is running. */
		ribbon: { kind: AnalysisKind; label: string; progress: number } | null;
		/** Staged ingest batches awaiting a manual Rekordbox import. */
		pendingBatches: { name: string; dest_dir: string }[];
		rbPopoverOpen: boolean;
		onToggleQueue: () => void;
		onToggleRbPopover: () => void;
		onConfirmBatch: (name: string) => void;
		queuePanel: Snippet;
	} = $props();

	const rbLabel = $derived(
		pendingBatches.length > 1
			? `awaiting Rekordbox import (${pendingBatches.length})`
			: 'awaiting Rekordbox import'
	);
	const rbTitle = $derived(pendingBatches.map((b) => b.dest_dir).join('\n'));
</script>

{#if ribbon}
	<button
		type="button"
		class="job-ribbon"
		style={`--job:${ANALYSIS_COLORS[ribbon.kind]}; --pct:${Math.max(0.02, ribbon.progress)}`}
		title={`${ribbon.label} offload. Click to open the job queue.`}
		aria-label={ribbon.label}
		onclick={() => onToggleQueue()}
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
			onclick={() => onToggleRbPopover()}
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
							onclick={() => onConfirmBatch(batch.name)}
						>
							Rekordbox import done
						</button>
					</div>
				{/each}
			</div>
		{/if}
	</div>
{/if}
{@render queuePanel()}

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
	.job-ribbon-label {
		position: relative;
		padding: 0 6px;
		font-size: 11px;
	}
	.rb-pending-wrap {
		position: relative;
		display: inline-flex;
	}
	.rb-pending-chip {
		display: inline-flex;
		align-items: center;
		/* 13px, not 16px: centered in the 17px bottom bar, a 16px chip sat
		   0.5px under the bar's border-top and the two lines read as one 2px
		   line. 13px leaves 2px of panel above and below (UIBORDER-02). */
		height: 13px;
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
