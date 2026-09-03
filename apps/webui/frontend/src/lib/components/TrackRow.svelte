<script lang="ts">
	import type { Track } from '$lib/api';
	let {
		track,
		selected = false,
		onclick,
		ontemporefedit
	}: {
		track: Track;
		selected?: boolean;
		onclick?: () => void;
		ontemporefedit?: (stableId: string, x: number, y: number) => void;
	} = $props();

	function _openTempoPrefEdit(event: MouseEvent): void {
		event.preventDefault();
		event.stopPropagation();
		ontemporefedit?.(track.stable_id, event.clientX, event.clientY);
	}
</script>

<tr onclick={onclick} class:selected data-stable-id={track.stable_id}>
	<td>{track.title ?? '(untitled)'}</td>
	<td>{track.artist ?? ''}</td>
	<td
		class="c-bpm"
		oncontextmenu={_openTempoPrefEdit}
		title="right-click to set preferred tempo / playable range"
	>{track.bpm ?? ''}</td>
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
