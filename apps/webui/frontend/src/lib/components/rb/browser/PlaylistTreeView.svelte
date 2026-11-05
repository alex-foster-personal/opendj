<script lang="ts">
	// PlaylistTree's panel as props only. The container (PlaylistTree.svelte)
	// owns the drag rules, the inline rename, the CURRENT fold tracker, the
	// page prefetch and the tree context menu; this draws the rows it is
	// handed. ColumnBrowser, RecentlyDeletedFolder and the context menu read
	// the backend themselves, so they come in as snippets.
	import type { Snippet } from 'svelte';
	import type { Action } from 'svelte/action';
	import type { PlaylistNode } from '$lib/rb/library-types';
	import type { PlaylistTreeViewMode } from '$lib/rb/playlist-tree-view-prefs';
	import { PENCIL_PATH } from '$lib/ui/icon-glyphs';
	import { playlistThumbnailUrl, SIDEBAR_TILE_PX } from '$lib/rb/library-sidebar-art-url';
	import type { PlaylistTint } from './playlist-context';
	import TreeCurrentFold from './TreeCurrentFold.svelte';
	import MissingTracksFolder from './MissingTracksFolder.svelte';
	import { MISSING_TRACKS_ID } from './missing-tracks-id';
	import { isInboxPlaylistId } from './inbox-id';
	import PlaylistFolderStates from './PlaylistFolderStates.svelte';
	import PlaylistHiddenBrokenNotice from './PlaylistHiddenBrokenNotice.svelte';
	import { allTracksBadge } from './all-tracks-badge';
	import { collapse } from '$lib/motion/collapse';

	type MenuKind = 'playlist' | 'folder';

	let {
		nodes,
		mode = 'tree',
		playlistsLoading,
		playlistsError,
		hiddenBrokenPlaylistCount,
		allTracksCount,
		allTracksBrokenCount,
		allTracksError,
		allTracksAvailability,
		allNode,
		selectedId,
		mostlyBrokenTitle,
		tintOf,
		columnMounted,
		dropTargetId,
		folderDropActive,
		playlistsOpen = $bindable(true),
		foldCurrent,
		scrollEl = $bindable(null),
		viewportHeight = $bindable(0),
		selectedRowAction,
		editingId,
		editDraft,
		createdId,
		renameInputEl = $bindable(null),
		cancreate,
		canrename,
		sidebarart = false,
		ondeleteplaylist,
		onselect,
		onselectmissing,
		oncreate,
		onbeginrename,
		oneditdraft,
		onrenamekeydown,
		onrenamecommit,
		onmenu,
		onmenukey,
		onprefetch,
		onscroll,
		onjump,
		onfolderdragover,
		onfolderdragleave,
		onfolderdrop,
		onrowdragstart,
		onrowdragover,
		onrowdragleave,
		onrowdrop,
		contextMenu,
		columnBrowser,
		recentlyDeleted,
		movingPlaylistId = null,
		reorderTarget = null,
		onrowdragend = undefined
	}: {
		nodes: PlaylistNode[];
		mode?: PlaylistTreeViewMode;
		playlistsLoading: boolean;
		playlistsError: string | null;
		hiddenBrokenPlaylistCount: number;
		allTracksCount: number | null;
		allTracksBrokenCount: number | null;
		allTracksError: string | null;
		/** Reconcile summary availability buckets (issue #3934). */
		allTracksAvailability: unknown;
		/** The All Tracks row's node, handed to onselect. */
		allNode: PlaylistNode;
		selectedId: string | null;
		/** Hover title of a mostly-broken playlist row. */
		mostlyBrokenTitle: string;
		/** Deck / multi-pane membership tint of a playlist row. */
		tintOf: (node: PlaylistNode) => PlaylistTint;
		/** ColumnBrowser has activated once and stays mounted (hidden in tree mode). */
		columnMounted: boolean;
		/** playlist_id currently under a track drag, for the drop outline. */
		dropTargetId: string | null;
		folderDropActive: boolean;
		playlistsOpen?: boolean;
		foldCurrent: 'above' | 'below' | null;
		scrollEl?: HTMLDivElement | null;
		viewportHeight?: number;
		/** Marks the selected row for the CURRENT fold tracker. */
		selectedRowAction: Action<HTMLElement, boolean>;
		/** playlist_id being renamed inline, or null. */
		editingId: string | null;
		editDraft: string;
		/** playlist_id of the latest create-then-rename, for the "what next" hint. */
		createdId: string | null;
		renameInputEl?: HTMLInputElement | null;
		cancreate: boolean;
		canrename: boolean;
		/** ARTTHUMB-8728c: show a 48 px playlist art tile and a by-line per row. */
		sidebarart?: boolean;
		ondeleteplaylist?: ((node: PlaylistNode) => void) | undefined;
		onselect: (node: PlaylistNode, opts?: { newTab?: boolean }) => void;
		onselectmissing: () => void;
		oncreate: () => void;
		onbeginrename: (node: PlaylistNode) => void;
		oneditdraft: (value: string) => void;
		onrenamekeydown: (event: KeyboardEvent) => void;
		onrenamecommit: () => void;
		onmenu: (event: MouseEvent, kind: MenuKind, node?: PlaylistNode) => void;
		/** Every folder and playlist row keydown, for the menu's keyboard opener. */
		onmenukey: (event: KeyboardEvent, kind: MenuKind, node?: PlaylistNode) => void;
		/** Pointer intent on a playlist row: warms its first page. */
		onprefetch: (playlistId: string) => void;
		onscroll: (event: Event) => void;
		onjump: () => void;
		onfolderdragover: (event: DragEvent) => void;
		onfolderdragleave: (event: DragEvent) => void;
		onfolderdrop: (event: DragEvent) => void;
		onrowdragstart: (event: DragEvent, node: PlaylistNode) => void;
		onrowdragover: (event: DragEvent, node: PlaylistNode) => void;
		onrowdragleave: (node: PlaylistNode) => void;
		onrowdrop: (event: DragEvent, node: PlaylistNode) => void;
		contextMenu: Snippet;
		columnBrowser: Snippet;
		recentlyDeleted: Snippet;
		/** LIBTREE-01: the playlist row being dragged to a new place, or null. */
		movingPlaylistId?: string | null;
		/** LIBTREE-01: where the dragged playlist would land, or null. */
		reorderTarget?: { id: string; place: import('./playlist-tree-order').DropPlace } | null;
		/** LIBTREE-01: clears the reorder drag when the dragged row ends. */
		onrowdragend?: (() => void) | undefined;
	} = $props();

	// #3934: the same playable predicate as the playlist badges below it.
	const allBadge = $derived(allTracksBadge(allTracksCount, allTracksBrokenCount, allTracksAvailability));

	function _allTracksCountTitle(): string {
		if (allTracksError !== null) return `track count unavailable: ${allTracksError}`;
		return allBadge.title;
	}

	function _playlistCountTitle(node: PlaylistNode): string {
		return `${node.track_count - node.broken_count} non-broken tracks, ${node.broken_count} broken tracks`;
	}

	function _rowKeydown(event: KeyboardEvent, node: PlaylistNode): void {
		if (event.key === 'Enter') onselect(node);
	}
</script>

<div class="tree-root" data-testid="playlist-tree-panel">
	{@render contextMenu()}
	{#if columnMounted}
		<div
			class="tree-scroll column-mode"
			class:hidden={mode !== 'column'}
			data-testid="playlist-column-view"
		>
			{@render columnBrowser()}
		</div>
	{/if}
	<div
		class="tree-scroll"
		class:hidden={mode === 'column'}
		class:has-fold={foldCurrent !== null}
		class:folder-drop-target={folderDropActive}
		bind:this={scrollEl}
		bind:clientHeight={viewportHeight}
		{onscroll}
		ondragover={onfolderdragover}
		ondragleave={onfolderdragleave}
		ondrop={onfolderdrop}
	>
		<TreeCurrentFold fold={foldCurrent} {onjump} />
		<div
			class="row"
			class:selected={selectedId === 'all'}
			data-testid="playlist-all-tracks"
			role="button"
			tabindex="0"
			onclick={(e) => onselect(allNode, { newTab: e.metaKey || e.ctrlKey })}
			onkeydown={(e) => _rowKeydown(e, allNode)}
		>
			<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
				<path
					d="M6 3l7-1v8.5a2 2 0 1 1-1-1.73V4.2L7 5v6.5a2 2 0 1 1-1-1.73z"
					fill="currentColor"
				/>
			</svg>
			<span class="name">All Tracks</span>
			<span class="count" title={_allTracksCountTitle()}>{allTracksError === null && allBadge.error === null ? allBadge.count ?? '...' : '!'}</span>
		</div>
		<div
			class="row folder"
			data-testid="playlist-folder"
			role="button"
			tabindex="0"
			onclick={() => (playlistsOpen = !playlistsOpen)}
			onkeydown={(e) => {
				if (e.key === 'Enter') playlistsOpen = !playlistsOpen;
				onmenukey(e, 'folder');
			}}
			oncontextmenu={(e) => onmenu(e, 'folder')}
		>
			<span class="disclosure" class:open={playlistsOpen}>&#9656;</span>
			<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
				<path d="M1 3h5l1.5 2H15v8H1z" fill="currentColor" />
			</svg>
			<span class="name">Playlists</span>
			{#if sidebarart}
				<a
					class="pl-action"
					data-testid="playlists-grid-link"
					href="/playlists"
					title="Show every playlist as a card with its art"
					aria-label="Show playlists as a grid"
					onclick={(e) => e.stopPropagation()}
				>
					<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true"><path d="M1 1h6v6H1zM9 1h6v6H9zM1 9h6v6H1zM9 9h6v6H9z" fill="currentColor" /></svg>
				</a>
			{/if}
			{#if cancreate}
				<button
					type="button"
					class="pl-action"
					title="Create playlist"
					onclick={(e) => {
						e.stopPropagation();
						oncreate();
					}}
				>
					+
				</button>
			{/if}
		</div>
		{#if playlistsOpen}
			<div class="collapse" in:collapse out:collapse>
				<div class="collapse-inner">
					<PlaylistFolderStates
						nodeCount={nodes.length}
						{playlistsLoading}
						{playlistsError}
						{hiddenBrokenPlaylistCount}
						oncreate={cancreate ? () => oncreate() : undefined}
					/>
					{#each nodes as node (node.playlist_id)}
						<div
							class="row child"
							data-testid="playlist-row"
							class:selected={selectedId === node.playlist_id}
							class:broken={node.mostly_broken}
							title={node.mostly_broken ? mostlyBrokenTitle : undefined}
							class:drop-target={dropTargetId === node.playlist_id}
							class:drop-before={reorderTarget?.id === node.playlist_id && reorderTarget.place === 'before'}
							class:drop-after={reorderTarget?.id === node.playlist_id && reorderTarget.place === 'after'}
							class:moving={movingPlaylistId === node.playlist_id}
							class:tint-deck={tintOf(node) === 'deck'}
							class:tint-multi={tintOf(node) === 'multi'}
							class:art={sidebarart}
							role="button"
							tabindex="0"
							draggable="true"
							use:selectedRowAction={selectedId === node.playlist_id}
							onpointerenter={() => { if (node.kind === 'playlist') onprefetch(node.playlist_id); }}
							onpointerdown={() => { if (node.kind === 'playlist') onprefetch(node.playlist_id); }}
							onclick={(e) => onselect(node, { newTab: e.metaKey || e.ctrlKey })}
							onkeydown={(e) => { _rowKeydown(e, node); onmenukey(e, 'playlist', node); }}
							oncontextmenu={(e) => onmenu(e, 'playlist', node)}
							ondragstart={(e) => onrowdragstart(e, node)}
							ondragover={(e) => onrowdragover(e, node)}
							ondragleave={() => onrowdragleave(node)}
							ondrop={(e) => onrowdrop(e, node)}
							ondragend={() => onrowdragend?.()}
						>
							{#if sidebarart && node.kind === 'folder'}
								<span class="art-tile art-glyph" data-testid="playlist-art-folder" title="Folder" aria-hidden="true">
									<svg viewBox="0 0 24 24" width="22" height="22"><path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" fill="currentColor" /></svg>
								</span>
							{:else if sidebarart}
								<img
									class="art-tile"
									data-testid="playlist-art-tile"
									src={playlistThumbnailUrl(node.playlist_id, 64)}
									width={SIDEBAR_TILE_PX}
									height={SIDEBAR_TILE_PX}
									loading="lazy"
									decoding="async"
									alt=""
									title={`Playlist art for ${node.name}: the first four different covers, one cover, or a note when it has none`}
								/>
							{:else}
								<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
									<path d="M2 3h8v2H2zM2 7h8v2H2zM2 11h8v2H2zM11 5l4 3-4 3z" fill="currentColor" />
								</svg>
							{/if}
							{#if editingId === node.playlist_id}
								<input
									bind:this={renameInputEl}
									class="rename-input"
									type="text"
									value={editDraft}
									aria-label="Rename playlist"
									onclick={(e) => e.stopPropagation()}
									onmousedown={(e) => e.stopPropagation()}
									oninput={(e) => oneditdraft(e.currentTarget.value)}
									onkeydown={(e) => onrenamekeydown(e)}
									onblur={() => onrenamecommit()}
								/>
							{:else}
								{#if sidebarart}
									<span class="name-stack">
										<span class="name" title={node.mostly_broken ? mostlyBrokenTitle : node.name}>{node.name}</span>
										<small class="byline" title="What this row is and how many tracks it holds">{node.kind === 'folder' ? 'Folder' : `Playlist - ${node.track_count} tracks`}</small>
									</span>
								{:else}
									<span class="name" title={node.mostly_broken ? mostlyBrokenTitle : node.name}>{node.name}</span>
								{/if}
							{/if}
							{#if canrename && !isInboxPlaylistId(node.playlist_id)}
								<button
									type="button"
									class="pl-action dim"
									title="Rename playlist"
									aria-label="Rename playlist"
									onclick={(e) => {
										e.stopPropagation();
										onbeginrename(node);
									}}
								>
									<svg viewBox="0 0 24 24" width="11" height="11" aria-hidden="true">
										<path d={PENCIL_PATH} fill="none" stroke="currentColor" stroke-width="2" />
									</svg>
								</button>
							{/if}
							{#if ondeleteplaylist && !isInboxPlaylistId(node.playlist_id)}
								<button
									type="button"
									class="pl-action dim"
									title="Delete playlist"
									onclick={(e) => {
										e.stopPropagation();
										ondeleteplaylist(node);
									}}
								>
									×
								</button>
							{/if}
							{#if selectedId === node.playlist_id}
								<span class="badge badge-plus" aria-hidden="true">+</span>
							{/if}
							<span class="count" title={_playlistCountTitle(node)}>{node.track_count - node.broken_count}</span>
						</div>
						{#if createdId === node.playlist_id && node.track_count === 0}
							<div class="row child hint" data-testid="playlist-new-hint">
								Drag tracks or a folder here to add music
							</div>
						{/if}
					{/each}
					<PlaylistHiddenBrokenNotice {hiddenBrokenPlaylistCount} />
				</div>
			</div>
		{/if}
		{@render recentlyDeleted()}
		<!-- data-testid="playlist-missing-tracks" is on MissingTracksFolder -->
		<MissingTracksFolder
			brokenCount={allTracksBrokenCount}
			error={allTracksError}
			selected={selectedId === MISSING_TRACKS_ID}
			onselect={onselectmissing}
		/>
	</div>
</div>

<style>
	/* Shared collapse rule ($lib/motion/collapse.ts): the transition animates
	 * this grid's single row from 0fr to 1fr; the inner block clips while it does. */
	.collapse {
		display: grid;
	}
	.collapse-inner {
		min-height: 0;
		overflow: hidden;
	}
	.tree-root {
		position: relative;
		display: flex;
		flex-direction: column;
		min-height: 0;
		height: 100%;
	}
	/* Pin 2ac3a0: deck-membership tints for a non-selected playlist row - the
	 * selection colour (.row.selected) always wins over both, and the darker
	 * multi-tab tint outranks the lighter loaded-deck tint (see
	 * playlistTintOf's precedence in pane-contract.svelte.ts). */
	.row.child.tint-deck:not(.selected) {
		background: color-mix(in srgb, var(--rb-accent) 14%, transparent);
	}
	.row.child.tint-multi:not(.selected) {
		background: color-mix(in srgb, var(--rb-accent) 30%, transparent);
	}
	.row.child.tint-deck:not(.selected):hover,
	.row.child.tint-multi:not(.selected):hover {
		background: color-mix(in srgb, var(--rb-accent) 40%, transparent);
	}
	.tree-scroll {
		position: relative;
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		padding: 2px 0;
		z-index: 1;
	}
	.tree-scroll.has-fold {
		padding-top: 26px;
	}
	.tree-scroll.column-mode {
		/* ColumnBrowser owns its own column padding/scroll regions. */
		padding: 0;
		overflow: hidden;
	}
	/* Both tree-scroll blocks stay mounted once ColumnBrowser has first
	 * activated (see columnMounted) - toggling visibility this way instead
	 * of {#if}/{:else} keeps ColumnBrowser's fetched rows/selection alive
	 * across repeated view switches. */
	.tree-scroll.hidden {
		display: none;
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
	.row.broken {
		color: var(--rb-text-dim);
	}
	.row.drop-target {
		box-shadow: inset 0 0 0 1px var(--rb-accent);
	}
	/* LIBTREE-01: where a dragged playlist will land. */
	.row.drop-before {
		box-shadow: inset 0 2px 0 var(--rb-accent);
	}
	.row.drop-after {
		box-shadow: inset 0 -2px 0 var(--rb-accent);
	}
	.row.moving {
		opacity: 0.5;
	}
	.tree-scroll.folder-drop-target {
		box-shadow: inset 0 0 0 2px var(--rb-accent);
	}
	.row svg {
		flex: none;
		color: var(--rb-text-dim);
	}
	.row.child {
		padding-left: 22px;
	}
	/* ARTTHUMB-8728c variant A: 48 px tile, 4 px corners, two-line by-line. The
	 * tile has fixed width and height so a late image never shifts a row. */
	.row.child.art {
		height: 56px;
		padding-left: 8px;
		gap: 10px;
	}
	.art-tile {
		flex: none;
		width: 48px;
		height: 48px;
		border-radius: 4px;
		object-fit: cover;
		background: var(--rb-panel-raised);
	}
	.art-glyph {
		display: grid;
		place-items: center;
		color: var(--rb-text-dim);
	}
	.name-stack {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		line-height: 1.3;
	}
	.byline {
		color: var(--rb-text-dim);
		font-size: 0.85em;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.row.hint {
		color: var(--rb-text-dim);
		font-style: italic;
		cursor: default;
		pointer-events: none;
	}
	.row.hint:hover {
		background: transparent;
	}
	.name {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.rename-input {
		flex: 1;
		min-width: 0;
		height: 16px;
		margin: 0;
		padding: 0 2px;
		border: 1px solid var(--rb-accent);
		background: var(--rb-panel);
		color: var(--rb-text);
		font: inherit;
		outline: none;
	}
	.pl-action {
		flex: none;
		border: none;
		background: transparent;
		color: var(--rb-text);
		font-size: 12px;
		line-height: 1;
		padding: 0 3px;
		cursor: pointer;
		opacity: 0.75;
	}
	.pl-action.dim {
		opacity: 0;
	}
	.row:hover .pl-action.dim,
	.row.selected .pl-action.dim {
		opacity: 0.7;
	}
	.pl-action:hover {
		opacity: 1 !important;
		color: var(--rb-accent);
	}
	.count {
		flex: none;
		min-width: 4ch;
		align-self: stretch;
		display: flex;
		align-items: center;
		justify-content: flex-end;
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
	.badge {
		flex: none;
		font-size: 8px;
		line-height: 1.3;
		border-radius: 2px;
		padding: 0 3px;
	}
	.badge-plus {
		color: var(--rb-on-accent, #fff);
		background: var(--rb-accent);
		font-weight: 600;
	}
	.row.rb-inert {
		opacity: 0.5;
		cursor: default;
	}
	.row.rb-inert:hover {
		background: transparent;
	}
	.name.dim,
	.name.error {
		color: var(--rb-text-dim);
	}
	.gear {
		flex: none;
	}
</style>
