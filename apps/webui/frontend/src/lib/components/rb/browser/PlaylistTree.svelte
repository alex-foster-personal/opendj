<script lang="ts">
	// Playlist tree panel (SCREENSHOT-SPEC 5b). Tree View tab active; Column
	// View locked/dim (inert). 'All Tracks' shows OUR live total (never the
	// screenshot's 9862); 'Playlists' folder lists real playlists with
	// right-aligned counts in rekordbox custom tree order (djmdPlaylist Seq,
	// sorted upstream in BrowserPanel - COMPONENT-MAP 1.5).
	import type { PlaylistNode } from '$lib/rb/types';

	let {
		nodes,
		allTracksCount,
		selectedId,
		onselect
	}: {
		nodes: PlaylistNode[];
		allTracksCount: number | null;
		selectedId: string | null;
		onselect: (node: PlaylistNode) => void;
	} = $props();

	let playlistsOpen = $state(true);

	const allNode = $derived<PlaylistNode>({
		playlist_id: 'all',
		name: 'All Tracks',
		track_count: allTracksCount ?? 0,
		kind: 'all_tracks',
		children: []
	});

	function _rowKeydown(event: KeyboardEvent, node: PlaylistNode): void {
		if (event.key === 'Enter') onselect(node);
	}
</script>

<div class="tree-root">
	<div class="view-tabs">
		<button class="vt active">Tree View</button>
		<button class="vt rb-inert" disabled title="not implemented - see PARITY-TODO">
			Column View
		</button>
	</div>
	<div class="tree-scroll">
		<div
			class="row"
			class:selected={selectedId === 'all'}
			role="button"
			tabindex="0"
			onclick={() => onselect(allNode)}
			onkeydown={(e) => _rowKeydown(e, allNode)}
		>
			<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
				<path
					d="M6 3l7-1v8.5a2 2 0 1 1-1-1.73V4.2L7 5v6.5a2 2 0 1 1-1-1.73z"
					fill="currentColor"
				/>
			</svg>
			<span class="name">All Tracks</span>
			<span class="count">{allTracksCount ?? '...'}</span>
		</div>
		<div
			class="row folder"
			role="button"
			tabindex="0"
			onclick={() => (playlistsOpen = !playlistsOpen)}
			onkeydown={(e) => {
				if (e.key === 'Enter') playlistsOpen = !playlistsOpen;
			}}
		>
			<span class="disclosure" class:open={playlistsOpen}>&#9656;</span>
			<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
				<path d="M1 3h5l1.5 2H15v8H1z" fill="currentColor" />
			</svg>
			<span class="name">Playlists</span>
		</div>
		{#if playlistsOpen}
			{#each nodes as node (node.playlist_id)}
				<div
					class="row child"
					class:selected={selectedId === node.playlist_id}
					role="button"
					tabindex="0"
					onclick={() => onselect(node)}
					onkeydown={(e) => _rowKeydown(e, node)}
				>
					<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
						<path d="M2 3h8v2H2zM2 7h8v2H2zM2 11h8v2H2zM11 5l4 3-4 3z" fill="currentColor" />
					</svg>
					<span class="name" title={node.name}>{node.name}</span>
					<span class="count">{node.track_count}</span>
				</div>
			{/each}
		{/if}
	</div>
</div>

<style>
	.tree-root {
		display: flex;
		flex-direction: column;
		min-height: 0;
		height: 100%;
	}
	.view-tabs {
		display: flex;
		flex: none;
		border-bottom: 1px solid var(--rb-border);
	}
	.vt {
		flex: 1;
		padding: 3px 0;
		background: var(--rb-panel);
		border: none;
		border-right: 1px solid var(--rb-border);
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		cursor: pointer;
	}
	.vt:last-child {
		border-right: none;
	}
	.vt.active {
		color: var(--rb-text);
		background: var(--rb-panel-raised);
		border-bottom: 1px solid var(--rb-accent);
	}
	.tree-scroll {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		padding: 2px 0;
	}
	.row {
		display: flex;
		align-items: center;
		gap: 5px;
		height: 20px;
		padding: 0 6px;
		color: var(--rb-text);
		cursor: pointer;
		white-space: nowrap;
	}
	.row:hover {
		background: var(--rb-panel-raised);
	}
	.row.selected {
		background: var(--rb-select);
	}
	.row svg {
		flex: none;
		color: var(--rb-text-dim);
	}
	.row.child {
		padding-left: 22px;
	}
	.name {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.count {
		flex: none;
		color: var(--rb-text-dim);
		font-variant-numeric: tabular-nums;
	}
	.disclosure {
		flex: none;
		display: inline-block;
		width: 8px;
		color: var(--rb-text-dim);
		transition: transform 0.1s;
	}
	.disclosure.open {
		transform: rotate(90deg);
	}
</style>
