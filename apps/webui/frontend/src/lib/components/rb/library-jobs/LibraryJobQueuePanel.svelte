<script lang="ts">
	import { onMount } from 'svelte';
	import {
		cancelLibraryJob,
		listLibraryJobs,
		reorderLibraryJob,
		type LibraryJobItem,
		type LibraryJobLane
	} from '$lib/rb/api-library-jobs';
	import { libraryJobsStore } from '$lib/rb/library-jobs-store.svelte';

	let { open = $bindable(false) }: { open?: boolean } = $props();
	let lane: LibraryJobLane = $state('stems');
	let error: string | null = $state(null);
	let dragId: string | null = $state(null);

	const items = $derived(libraryJobsStore.items(lane));
	const pending = $derived(items.filter((i) => i.state === 'pending'));
	const running = $derived(items.filter((i) => i.state === 'running'));

	onMount(() => {
		libraryJobsStore.attach();
		return () => libraryJobsStore.detach();
	});

	function chipTitle(item: LibraryJobItem): string {
		if (item.state === 'pending') return `${item.lane} queued. Drag to reorder or cancel.`;
		if (item.state === 'running') return `${item.lane} running. Cancel stops at the next safe point.`;
		if (item.state === 'skipped') return item.detail ?? 'skipped: up to date';
		if (item.state === 'cancelled') return `${item.lane} cancelled.`;
		if (item.state === 'failed') return item.detail ?? `${item.lane} failed.`;
		return `${item.lane} ${item.state}.`;
	}

	async function onCancel(item: LibraryJobItem): Promise<void> {
		error = null;
		try {
			await cancelLibraryJob(item.lane, item.stable_id);
			await libraryJobsStore.refresh();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		}
	}

	async function onDrop(beforeId: string | null): Promise<void> {
		if (dragId === null || dragId === beforeId) return;
		error = null;
		try {
			await reorderLibraryJob(lane, dragId, beforeId);
			await listLibraryJobs(lane);
			await libraryJobsStore.refresh();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			dragId = null;
		}
	}
</script>

{#if open}
	<section class="panel" aria-label="Library job queue">
		<header>
			<strong>Library jobs</strong>
			<button type="button" class="tab" class:on={lane === 'stems'} onclick={() => (lane = 'stems')} title="Stems lane: separate from lyrics, one running job">Stems</button>
			<button type="button" class="tab" class:on={lane === 'lyrics'} onclick={() => (lane = 'lyrics')} title="Lyrics lane: separate from stems, one running job">Lyrics</button>
			<button type="button" class="close" onclick={() => (open = false)} title="Close job queue" aria-label="Close job queue">x</button>
		</header>
		{#if error}<p class="err">{error}</p>{/if}
		<ul>
			{#each running as item (item.stable_id)}
				<li>
					<span class="chip run" title={chipTitle(item)}>{item.stable_id} running</span>
					<button type="button" onclick={() => onCancel(item)} title="Cancel running job at the next safe point">Cancel</button>
				</li>
			{/each}
			{#each pending as item, i (item.stable_id)}
				<li
					draggable="true"
					ondragstart={() => (dragId = item.stable_id)}
					ondragover={(e) => e.preventDefault()}
					ondrop={() => onDrop(item.stable_id)}
				>
					<span class="chip q" title={chipTitle(item)}>{i + 1}. {item.stable_id} queued</span>
					<button type="button" onclick={() => onCancel(item)} title="Cancel queued job; it leaves the queue">Cancel</button>
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
	</section>
{/if}

<style>
	.panel { position: absolute; bottom: 28px; left: 8px; z-index: 20; width: 320px; background: #1b1d22; color: #eee; border: 1px solid #444; padding: 8px; font-size: 12px; }
	header { display: flex; gap: 6px; align-items: center; margin-bottom: 6px; }
	.tab { background: transparent; color: inherit; border: 1px solid #555; }
	.tab.on { border-color: #9cf; }
	.close { margin-left: auto; }
	ul { list-style: none; margin: 0; padding: 0; max-height: 240px; overflow: auto; }
	li { display: flex; justify-content: space-between; gap: 8px; padding: 4px 0; }
	.chip { padding: 1px 4px; }
	.chip.run { color: #9cf; }
	.chip.q { color: #ccc; }
	.err, .empty { color: #c88; }
	.tail { margin-top: 6px; border: 1px dashed #555; padding: 4px; text-align: center; color: #888; }
</style>
