<script lang="ts">
	import type { PlaylistSummaryStatsDoc } from '$lib/rb/api-playlist-summary';
	import { formatPlaylistSummaryLine, playlistSummaryTitle } from '$lib/rb/playlist-summary-format';

	let { stats }: { stats: PlaylistSummaryStatsDoc | null } = $props();

	const line = $derived(stats === null ? '' : formatPlaylistSummaryLine(stats));
	const title = $derived(stats === null ? '' : playlistSummaryTitle(stats));
</script>

{#if stats !== null}
	<span class="playlist-summary-line" data-testid="playlist-summary-line" {title}>{line}</span>
{/if}

<style>
	.playlist-summary-line {
		color: var(--rb-text-dim, #9aa0a6);
		font-size: 11px;
		white-space: nowrap;
		padding: 0 6px;
	}
</style>
