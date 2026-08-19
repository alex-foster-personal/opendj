<script lang="ts">
	import { onMount } from 'svelte';
	import { listTracks, type Track } from '$lib/api';
	import VirtualTable from '$lib/components/VirtualTable.svelte';
	import FirstRunOverlay from '$lib/components/rb/FirstRunOverlay.svelte';
	import { resolveFirstRun } from '$lib/setup/first-run';
	import { pushToast } from '$lib/stores.svelte';

	let showFirstRun = $state(false);
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

	/**
	 * The first-run gate. An empty library that has not been dismissed dims
	 * the page and offers the wizard, instead of showing an empty table with
	 * no explanation -- and instead of navigating away, which is what this
	 * used to do. The app stays on screen behind the ask.
	 *
	 * The daemon decides, not the browser: `should_show_wizard` is computed
	 * engine-side (and is already false for a developer checkout), so a
	 * reload, a second tab and an agent all get the same answer. The rule
	 * itself lives in $lib/setup/first-run, under test.
	 */
	onMount(() => {
		void resolveFirstRun().then((show) => {
			showFirstRun = show;
		});
		void fetchPage();
	});

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

{#if showFirstRun}
	<FirstRunOverlay />
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
