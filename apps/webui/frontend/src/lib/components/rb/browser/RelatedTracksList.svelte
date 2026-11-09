<script lang="ts">
	// LIBUX-10046: the Related tab's list (rekordbox Related Tracks). Rows drag
	// to a deck like any library row; double-click loads.
	import { TRACK_STABLE_MIME } from '$lib/rb/track-drag.svelte';
	import type { RelatedTracksState } from './related-tracks.svelte';

	let {
		state,
		onload,
		onplay
	}: {
		state: RelatedTracksState;
		onload: (stableId: string) => void;
		onplay: (stableId: string) => void;
	} = $props();

	function _onDragStart(event: DragEvent, stableId: string): void {
		event.dataTransfer?.setData(TRACK_STABLE_MIME, stableId);
		if (event.dataTransfer !== null) event.dataTransfer.effectAllowed = 'copy';
	}
</script>

<div class="related-list" data-testid="related-tracks-list">
	{#if state.kind === 'idle'}
		<div class="related-note">Select a track, or load one to a deck, to see related tracks</div>
	{:else if state.kind === 'loading'}
		<div class="related-note">Finding related tracks...</div>
	{:else if state.kind === 'error'}
		<div class="related-error" role="alert">{state.message}</div>
	{:else if state.items.length === 0}
		<div class="related-note">No related tracks for {state.seedTitle}</div>
	{:else}
		<table>
			<thead>
				<tr
					><th>Title</th><th>Artist</th><th>BPM</th><th>Key</th><th>Genre</th><th>Score</th><th
						>Matched</th
					></tr
				>
			</thead>
			<tbody>
				{#each state.items as row (row.stable_id)}
					<tr
						draggable="true"
						ondragstart={(e) => _onDragStart(e, row.stable_id)}
						ondblclick={(e) => (e.shiftKey ? onplay(row.stable_id) : onload(row.stable_id))}
					>
						<td>{row.title ?? ''}</td>
						<td>{row.artist ?? ''}</td>
						<td>{row.bpm ?? ''}</td>
						<td>{row.key ?? ''}</td>
						<td>{row.genre ?? ''}</td>
						<td>{row.score}</td>
						<td>{row.matched.join(', ')}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
</div>

<style>
	.related-list {
		flex: 1;
		overflow: auto;
		font-size: 11px;
	}

	.related-note,
	.related-error {
		padding: 8px;
		opacity: 0.8;
	}

	.related-error {
		color: var(--rb-danger, #f66);
		opacity: 1;
	}

	table {
		width: 100%;
		border-collapse: collapse;
	}

	th,
	td {
		text-align: left;
		padding: 2px 6px;
		white-space: nowrap;
	}

	tbody tr:hover {
		background: var(--rb-row-hover, rgba(255, 255, 255, 0.06));
	}
</style>
