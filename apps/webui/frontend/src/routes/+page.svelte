<script lang="ts">
	import { onMount } from 'svelte';
	import { listTracks, type Track } from '$lib/api';
	import VirtualTable from '$lib/components/VirtualTable.svelte';
	import { pushToast } from '$lib/stores.svelte';

	let tracks = $state<Track[]>([]);
	let nextCursor = $state<string | null>(null);
	let loading = $state(false);
	let q = $state('');
	let bpmMin = $state('');
	let bpmMax = $state('');
	let ratingMin = $state('');

	async function fetchPage(cursor: string | null = null): Promise<void> {
		if (loading) return;
		loading = true;
		try {
			const page = await listTracks({
				q, bpm_min: bpmMin || null, bpm_max: bpmMax || null,
				rating_min: ratingMin || null, cursor, limit: 200
			});
			tracks = cursor ? [...tracks, ...page.items] : page.items;
			nextCursor = page.next_cursor;
		} catch (exc) {
			pushToast(`Failed to load tracks: ${exc}`, 'error');
		} finally {
			loading = false;
		}
	}

	onMount(() => fetchPage());

	let filterTimer: ReturnType<typeof setTimeout> | null = null;
	function onFilterChange(): void {
		if (filterTimer) clearTimeout(filterTimer);
		filterTimer = setTimeout(() => fetchPage(null), 200);
	}
</script>

<h2>Library</h2>
<form class="filters" onsubmit={(e) => e.preventDefault()}>
	<input placeholder="Search title/artist" bind:value={q} oninput={onFilterChange} />
	<input placeholder="BPM min" bind:value={bpmMin} oninput={onFilterChange} />
	<input placeholder="BPM max" bind:value={bpmMax} oninput={onFilterChange} />
	<input placeholder="Min rating" bind:value={ratingMin} oninput={onFilterChange} />
</form>

<VirtualTable {tracks} onload_more={() => nextCursor && fetchPage(nextCursor)} />

{#if loading}
	<p>Loading...</p>
{/if}

<style>
	.filters {
		display: flex;
		gap: 0.5rem;
		margin-bottom: 1rem;
	}
	.filters input {
		width: 160px;
	}
</style>
