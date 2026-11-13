<script lang="ts">
	// Props-only half of LibraryJobQueuePanel (PERFBATCH-05): the per-lane
	// queue with running and queued chips, cancel buttons and drag-to-reorder.
	// LibraryJobQueuePanel.svelte owns the store, the lane and the HTTP calls.
	import type { LibraryJobItem, LibraryJobLane } from '$lib/rb/api-library-jobs';

	const {
		open,
		lane,
		error,
		refusal = null,
		running,
		pending,
		onSelectLane,
		onClose,
		onCancel,
		onDragStart,
		onDrop
	}: {
		open: boolean;
		lane: LibraryJobLane;
		/** Last cancel or reorder failure, or null. */
		error: string | null;
		/** Why the queue is inert (daemon does not serve the routes), or null. */
		refusal?: string | null;
		running: LibraryJobItem[];
		/** Queued jobs in queue order. */
		pending: LibraryJobItem[];
		onSelectLane: (lane: LibraryJobLane) => void;
		onClose: () => void;
		onCancel: (item: LibraryJobItem) => void;
		onDragStart: (stableId: string) => void;
		/** Drop the dragged job before this one, or at the end for null. */
		onDrop: (beforeId: string | null) => void;
	} = $props();

	function chipTitle(item: LibraryJobItem): string {
		if (item.state === 'pending') return `${item.lane} queued. Drag to reorder or cancel.`;
		if (item.state === 'running')
			return `${item.lane} running. Cancel stops at the next safe point.`;
		if (item.state === 'skipped') return item.detail ?? 'skipped: up to date';
		if (item.state === 'cancelled') return `${item.lane} cancelled.`;
		if (item.state === 'failed') return item.detail ?? `${item.lane} failed.`;
		return `${item.lane} ${item.state}.`;
	}
</script>

{#if open}
	<section class="panel" aria-label="Library job queue" data-testid="library-job-queue-panel">
		<header>
			<strong>Library jobs</strong>
			<button
				type="button"
				class="tab"
				class:on={lane === 'stems'}
				onclick={() => onSelectLane('stems')}
				title="Stems lane: separate from lyrics, one running job">Stems</button
			>
			<button
				type="button"
				class="tab"
				class:on={lane === 'lyrics'}
				onclick={() => onSelectLane('lyrics')}
				title="Lyrics lane: separate from stems, one running job">Lyrics</button
			>
			<button
				type="button"
				class="close"
				onclick={() => onClose()}
				title="Close job queue"
				aria-label="Close job queue">x</button
			>
		</header>
		{#if refusal}
			<p class="err" data-testid="library-job-refusal" title={refusal}>{refusal}</p>
		{:else}
			{#if error}<p class="err">{error}</p>{/if}
			<ul>
				{#each running as item (item.stable_id)}
					<li data-testid="library-job-row" data-stable-id={item.stable_id}>
						<span class="chip run" title={chipTitle(item)}>{item.stable_id} running</span>
						<button
							type="button"
							onclick={() => onCancel(item)}
							title="Cancel running job at the next safe point">Cancel</button
						>
					</li>
				{/each}
				{#each pending as item, i (item.stable_id)}
					<li
						data-testid="library-job-row"
						data-stable-id={item.stable_id}
						draggable="true"
						ondragstart={() => onDragStart(item.stable_id)}
						ondragover={(e) => e.preventDefault()}
						ondrop={() => onDrop(item.stable_id)}
					>
						<span class="chip q" title={chipTitle(item)}>{i + 1}. {item.stable_id} queued</span>
						<button
							type="button"
							onclick={() => onCancel(item)}
							title="Cancel queued job; it leaves the queue">Cancel</button
						>
					</li>
				{/each}
				{#if pending.length === 0 && running.length === 0}
					<li class="empty" title="No queued or running jobs in this lane">No jobs</li>
				{/if}
			</ul>
			<div
				class="tail"
				ondragover={(e) => e.preventDefault()}
				ondrop={() => onDrop(null)}
				title="Drop here to move a queued job to the end"
			>
				end
			</div>
		{/if}
	</section>
{/if}

<style>
	.panel {
		position: absolute;
		bottom: 28px;
		left: 8px;
		z-index: 20;
		width: 320px;
		background: #1b1d22;
		color: #eee;
		border: 1px solid #444;
		padding: 8px;
		font-size: 12px;
	}
	header {
		display: flex;
		gap: 6px;
		align-items: center;
		margin-bottom: 6px;
	}
	.tab {
		background: transparent;
		color: inherit;
		border: 1px solid #555;
	}
	.tab.on {
		border-color: #9cf;
	}
	.close {
		margin-left: auto;
	}
	ul {
		list-style: none;
		margin: 0;
		padding: 0;
		max-height: 240px;
		overflow: auto;
	}
	li {
		display: flex;
		justify-content: space-between;
		gap: 8px;
		padding: 4px 0;
	}
	.chip {
		padding: 1px 4px;
	}
	.chip.run {
		color: #9cf;
	}
	.chip.q {
		color: #ccc;
	}
	.err,
	.empty {
		color: #c88;
	}
	.tail {
		margin-top: 6px;
		border: 1px dashed #555;
		padding: 4px;
		text-align: center;
		color: #888;
	}
</style>
