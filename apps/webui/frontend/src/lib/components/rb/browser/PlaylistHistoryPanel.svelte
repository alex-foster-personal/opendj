<script lang="ts">
	import { onMount } from 'svelte';
	import { installPlaylistHistoryChrome } from '$lib/rb/playlist-history-chrome.svelte';

	onMount(() => installPlaylistHistoryChrome());
</script>

<!-- Headless: undo/redo UI lives on LibrarySourceTabs (issue #3983). -->
