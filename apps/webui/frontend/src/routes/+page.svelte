<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { listTracks, type Track } from '$lib/api';
	import VirtualTable from '$lib/components/VirtualTable.svelte';
	import { capabilities } from '$lib/api/capabilities.svelte';
	import { getSetupStatus, setupRefusal } from '$lib/setup/setup-api';
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

	/**
	 * The first-run gate. An empty library that has not been dismissed sends
	 * the user to the wizard instead of showing an empty table with no
	 * explanation.
	 *
	 * The daemon decides, not the browser: `should_show_wizard` is
	 * `library_empty && !dismissed` computed engine-side, so a reload, a
	 * second tab and an agent all get the same answer. A legacy boot has no
	 * setup API, so the gate simply does not run there.
	 */
	async function firstRunGate(): Promise<void> {
		await capabilities.probe();
		if (setupRefusal() !== null) return;
		try {
			if ((await getSetupStatus()).should_show_wizard) await goto('/setup');
		} catch (exc) {
			// Never blocks the library: a status probe that fails is a reason to
			// show the tracks, not to strand the user on a blank page.
			console.error('[library] first-run check failed', exc);
		}
	}

	onMount(() => {
		void firstRunGate();
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
