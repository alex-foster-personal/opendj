<script lang="ts">
	import type { Track } from '$lib/api';
	import TrackRow from './TrackRow.svelte';
	import { goto } from '$app/navigation';

	let {
		tracks,
		onload_more
	}: { tracks: Track[]; onload_more?: () => void } = $props();

	let selected = $state<number>(-1);

	function click(i: number): void {
		selected = i;
		goto(`/track/${tracks[i].stable_id}`);
	}

	function keydown(event: KeyboardEvent): void {
		if (event.key === 'ArrowDown') {
			selected = Math.min(tracks.length - 1, selected + 1);
			event.preventDefault();
		} else if (event.key === 'ArrowUp') {
			selected = Math.max(0, selected - 1);
			event.preventDefault();
		} else if (event.key === 'Enter' && selected >= 0) {
			click(selected);
		}
	}

	function scroll(event: Event): void {
		const el = event.currentTarget as HTMLElement;
		if (el.scrollHeight - el.scrollTop - el.clientHeight < 400) {
			onload_more?.();
		}
	}
</script>

<div class="table-wrap" onkeydown={keydown} onscroll={scroll} tabindex="0">
	<table class="library">
		<thead>
			<tr>
				<th>Title</th>
				<th>Artist</th>
				<th>BPM</th>
				<th>Key</th>
				<th>Rating</th>
				<th>Last played</th>
				<th>Tags</th>
			</tr>
		</thead>
		<tbody>
			{#each tracks as track, i}
				<TrackRow {track} selected={i === selected} onclick={() => click(i)} />
			{/each}
		</tbody>
	</table>
</div>

<style>
	.table-wrap {
		max-height: calc(100vh - 200px);
		overflow-y: auto;
	}
</style>
