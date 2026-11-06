<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { jobProgress } from '$lib/rb/job-progress.svelte';
	import { confirmIngestPending } from '$lib/rb/api-ingest';
	import { ingestPending, refreshIngestPending, startIngestPendingWatch, stopIngestPendingWatch } from '$lib/rb/ingest-pending.svelte';
	import LibraryJobQueuePanel from './LibraryJobQueuePanel.svelte';
	import LibraryJobsChromeView from './LibraryJobsChromeView.svelte';

	// The markup lives in LibraryJobsChromeView.svelte so it can carry a story.
	let open = $state(false);
	let rbPopoverOpen = $state(false);
	const ribbon = $derived(jobProgress.ribbon());
	const pendingBatches = $derived(ingestPending.batches);

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

<LibraryJobsChromeView
	{ribbon}
	{pendingBatches}
	{rbPopoverOpen}
	onToggleQueue={() => (open = !open)}
	onToggleRbPopover={() => (rbPopoverOpen = !rbPopoverOpen)}
	onConfirmBatch={(name) => void confirmBatch(name)}
>
	{#snippet queuePanel()}<LibraryJobQueuePanel bind:open />{/snippet}
</LibraryJobsChromeView>
