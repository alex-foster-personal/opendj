<script lang="ts">
	import { onMount } from 'svelte';
	import { fetchTrackLyrics, type TrackLyrics } from '$lib/rb/api-rb';
	import LineLyricsPanelView from './LineLyricsPanelView.svelte';

	let { stableId }: { stableId: string } = $props();

	let lyrics = $state<TrackLyrics | null>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);

	onMount(() => {
		void (async () => {
			loading = true;
			error = null;
			try {
				lyrics = await fetchTrackLyrics(stableId);
			} catch (exc) {
				error = exc instanceof Error ? exc.message : String(exc);
			} finally {
				loading = false;
			}
		})();
	});
</script>

<LineLyricsPanelView {lyrics} {loading} {error} />
