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
	import LibraryJobQueuePanelView from './LibraryJobQueuePanelView.svelte';

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

	async function onCancel(item: LibraryJobItem): Promise<void> {
		if (libraryJobsStore.refusal !== null) return;
		error = null;
		try {
			await cancelLibraryJob(item.lane, item.stable_id);
			await libraryJobsStore.refresh();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		}
	}

	async function onDrop(beforeId: string | null): Promise<void> {
		if (dragId === null || dragId === beforeId || libraryJobsStore.refusal !== null) return;
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

<LibraryJobQueuePanelView
	{open}
	{lane}
	{error}
	refusal={libraryJobsStore.refusal}
	{running}
	{pending}
	onSelectLane={(next) => (lane = next)}
	onClose={() => (open = false)}
	{onCancel}
	onDragStart={(stableId) => (dragId = stableId)}
	{onDrop}
/>
