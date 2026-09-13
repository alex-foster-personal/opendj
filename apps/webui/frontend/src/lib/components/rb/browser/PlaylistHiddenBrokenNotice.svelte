<script lang="ts">
	import { hiddenBrokenLabel } from './playlist-folder-labels';

	let { hiddenBrokenPlaylistCount }: { hiddenBrokenPlaylistCount: number } = $props();
</script>

{#if hiddenBrokenPlaylistCount > 0}
	<div class="row child rb-inert" data-testid="playlists-hidden-broken">
		{hiddenBrokenLabel(hiddenBrokenPlaylistCount)}
	</div>
{/if}

<style>
	.row {
		display: flex;
		align-items: center;
		gap: 5px;
		height: 20px;
		padding: 0 6px;
		color: var(--rb-text);
		white-space: nowrap;
	}
	.row.child {
		padding-left: 22px;
	}
	.row.rb-inert {
		opacity: 0.5;
		cursor: default;
	}
</style>
