<script lang="ts">
	import type { Track } from '$lib/api';
	import { STAR_FILLED_PATH, STAR_OUTLINE_PATH } from '$lib/ui/icon-glyphs';

	const STARS = [1, 2, 3, 4, 5];

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

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<tr onclick={onclick} class:selected data-stable-id={track.stable_id}>
	<td>{track.title ?? '(untitled)'}</td>
	<td>{track.artist ?? ''}</td>
	<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
	<td
		class="c-bpm"
		oncontextmenu={_openTempoPrefEdit}
		title="right-click to set preferred tempo / playable range"
	>{track.bpm ?? ''}</td>
	<td>{track.key ?? ''}</td>
	<td>
		{#if track.rating != null}
			<span class="rating-stars" aria-label={`rating ${track.rating}`}>
				{#each STARS as n (n)}
					<svg viewBox="0 0 24 24" width="10" height="10" aria-hidden="true">
						<path
							d={n <= track.rating ? STAR_FILLED_PATH : STAR_OUTLINE_PATH}
							fill={n <= track.rating ? 'currentColor' : 'none'}
							stroke="currentColor"
							stroke-width="1.2"
						/>
					</svg>
				{/each}
			</span>
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
	.rating-stars {
		display: inline-flex;
		gap: 1px;
	}
</style>
