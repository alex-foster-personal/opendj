<script lang="ts">
	import type { Track } from '$lib/api';
	let { track, selected = false, onclick }: { track: Track; selected?: boolean; onclick?: () => void } = $props();
</script>

<tr onclick={onclick} class:selected>
	<td>{track.title ?? '(untitled)'}</td>
	<td>{track.artist ?? ''}</td>
	<td>{track.bpm ?? ''}</td>
	<td>{track.key ?? ''}</td>
	<td>
		{#if track.rating != null}
			<span aria-label={`rating ${track.rating}`}>{'★'.repeat(track.rating)}{'☆'.repeat(5 - track.rating)}</span>
		{/if}
	</td>
	<td>{track.last_played_at ?? ''}</td>
	<td>
		{#each track.tags.slice(0, 3) as tag}
			<span class="chip">{tag}</span>
		{/each}
	</td>
</tr>

<style>
	tr.selected {
		background: #1a212c;
	}
</style>
