<script lang="ts">
	// Playlist tree panel (SCREENSHOT-SPEC 5b). 'All Tracks' shows OUR live
	// total (never the screenshot's 9862); 'Playlists' folder lists real
	// playlists with right-aligned counts in rekordbox custom tree order
	// (djmdPlaylist Seq, sorted upstream in BrowserPanel - COMPONENT-MAP
	// 1.5). The Column View tab (column-view lane) swaps this panel's body
	// for ColumnBrowser - self-contained (own library fetch), same pattern
	// as the smartlist self-fetch below.
	import type { PlaylistNode } from '$lib/rb/library-types';
	import { formatMostlyBrokenTooltip } from '$lib/rb/runtime-policy.svelte';
	import { PENCIL_PATH } from '$lib/ui/icon-glyphs';
	import ColumnBrowser from './ColumnBrowser.svelte';
	import type { PlaylistTreeProps } from './playlist-tree-props';
	import {
		acceptTrackDragOver,
		droppedStableIds,
		endTrackDrag
	} from '$lib/rb/track-drag.svelte';
	import { encodePlaylistDrag, PLAYLIST_DRAG_MIME } from './playlist-drag';
	import { trackDrag } from '$lib/rb/track-drag.svelte';
	import { isOsFileDrag } from '$lib/rb/ingest-drop-files';
	import { type PlaylistTint, playlistTintOf } from './pane-contract.svelte';
	import TreeCurrentFold from './TreeCurrentFold.svelte';
	import { TreeFoldTracker } from './tree-fold-tracker.svelte';
	import { TreePlaylistRename } from './tree-playlist-rename.svelte';
	import TreeContextMenu from './TreeContextMenu.svelte';
	import RecentlyDeletedFolder from './RecentlyDeletedFolder.svelte';
	import MissingTracksFolder from './MissingTracksFolder.svelte';
	import { MISSING_TRACKS_ID, missingTracksNode } from './missing-tracks';
	import PlaylistFolderStates from './PlaylistFolderStates.svelte';
	import PlaylistHiddenBrokenNotice from './PlaylistHiddenBrokenNotice.svelte';
	import type { PlaylistTreeViewMode } from '$lib/rb/playlist-tree-view-prefs';

	let {
		nodes,
		mode = 'tree',
		playlistsLoading,
		playlistsError,
		hiddenBrokenPlaylistCount,
		allTracksCount,
		allTracksBrokenCount,
		allTracksError,
		selectedId,
		trackSelectedId,
		deckLoadedPlaylistIds = new Set(),
		multiPanePlaylistIds = new Set(),
		onselect,
		onselectsmartlist,
		onselecttrack,
		onloadtrack,
		oncreateplaylist,
		onrenameplaylist,
		onforbidduplicates,
		ondeleteplaylist,
		onduplicateplaylist,
		ondroptracks,
		oncreatesmartlist,
		onfolderdrop
	}: PlaylistTreeProps & { mode?: PlaylistTreeViewMode } = $props();

	/** playlist_id currently under a track drag, for the drop outline. */
	let dropTargetId: string | null = $state(null);
	let folderDropActive = $state(false);
	let treeContextMenu = $state<TreeContextMenu | null>(null);

	function _onTrackDragOver(event: DragEvent, node: PlaylistNode): void {
		// All Tracks is a view, not a playlist, so it can never receive a drop.
		if (ondroptracks === undefined || node.kind !== 'playlist') return;
		// Same WKWebView rule as the deck drop targets: accept on the in-app
		// drag state, never on dataTransfer.types (see track-drag.svelte.ts).
		if (!acceptTrackDragOver(event)) return;
		dropTargetId = node.playlist_id;
	}

	function _onTrackDragLeave(node: PlaylistNode): void {
		if (dropTargetId === node.playlist_id) dropTargetId = null;
	}

	function _onTrackDrop(event: DragEvent, node: PlaylistNode): void {
		if (ondroptracks === undefined || node.kind !== 'playlist') return;
		event.preventDefault();
		dropTargetId = null;
		const ids = droppedStableIds(event);
		endTrackDrag();
		if (ids.length === 0) return;
		ondroptracks(node.playlist_id, ids);
	}

	function _onFolderDragOver(event: DragEvent): void {
		if (trackDrag.active || onfolderdrop === undefined || !isOsFileDrag(event)) return;
		event.preventDefault();
		event.stopPropagation();
		if (event.dataTransfer !== null) event.dataTransfer.dropEffect = 'copy';
		folderDropActive = true;
	}

	function _onFolderDragLeave(event: DragEvent): void {
		if (!isOsFileDrag(event)) return;
		folderDropActive = false;
	}

	function _onFolderDrop(event: DragEvent): void {
		if (trackDrag.active || onfolderdrop === undefined || !isOsFileDrag(event)) return;
		event.preventDefault();
		event.stopPropagation();
		folderDropActive = false;
		onfolderdrop(event);
	}

	/** Make a playlist row draggable onto the pane tab bar. */
	function _onPlaylistDragStart(event: DragEvent, node: PlaylistNode): void {
		if (
			node.kind === 'missing_tracks' ||
			node.kind === 'smartlist' ||
			node.kind === 'taglist' ||
			node.kind === 'autolist' ||
			node.kind === 'usb'
		)
			return;
		event.dataTransfer?.setData(
			PLAYLIST_DRAG_MIME,
			encodePlaylistDrag({
				playlist_id: node.playlist_id,
				name: node.name,
				track_count: node.track_count,
				kind: node.kind
			})
		);
		if (event.dataTransfer !== null) event.dataTransfer.effectAllowed = 'copy';
	}

	/** Inline playlist rename + create-then-rename flow (tree-playlist-rename.svelte.ts). */
	const rename = new TreePlaylistRename(
		() => nodes,
		() => onrenameplaylist,
		() => oncreateplaylist
	);

	// ColumnBrowser mounts lazily on first activation (its onMount walks
	// every /tracks cursor page - no point paying that for users who never
	// open column mode) but then STAYS mounted (visibility toggled via CSS
	// below, not {#if}/{:else}) so switching back to tree mode and back
	// doesn't re-trigger the full-library fetch every time.
	let columnMounted = $state(false);
	$effect(() => {
		if (mode === 'column') columnMounted = true;
	});
	let playlistsOpen = $state(true);

	const allNode = $derived<PlaylistNode>({
		playlist_id: 'all',
		name: 'All Tracks',
		track_count: allTracksCount ?? 0,
		broken_count: allTracksBrokenCount ?? 0,
		kind: 'all_tracks',
		children: []
	});

	function _allTracksCountTitle(): string {
		if (allTracksError !== null) return `non-broken count unavailable: ${allTracksError}`;
		if (allTracksCount === null || allTracksBrokenCount === null) {
			return 'loading non-broken and broken track counts';
		}
		return `${allTracksCount} unique library-wide non-broken tracks, ${allTracksBrokenCount} broken tracks`;
	}

	function _playlistCountTitle(node: PlaylistNode): string {
		return `${node.track_count - node.broken_count} non-broken tracks, ${node.broken_count} broken tracks`;
	}

	function _rowKeydown(event: KeyboardEvent, node: PlaylistNode): void {
		if (event.key === 'Enter') onselect(node);
	}

	$effect(() => {
		rename.checkPending();
	});

	function _tintOf(node: PlaylistNode): PlaylistTint {
		return playlistTintOf({
			playlist_id: node.playlist_id,
			selected: selectedId === node.playlist_id,
			deckLoadedPlaylistIds,
			multiPanePlaylistIds
		});
	}

	// -------------------------------------------------------- CURRENT fold (2ac3a0)
	// Mirrors TrackTable's MASTER-with-chevron fold (masterFold/jumpToMaster) so a
	// library with MANY playlists never loses track of which one is the active
	// pane's selection once it scrolls out of the tree's own viewport. The
	// tracking state + the button UI live in tree-fold-tracker.svelte.ts /
	// TreeCurrentFold.svelte; this component still owns the scrollable
	// container and the row elements the tracker watches, so it wires them
	// up via the tracker's exposed fields/action/handler.
	const foldTracker = new TreeFoldTracker();
</script>

<div class="tree-root" data-testid="playlist-tree-panel">
	<TreeContextMenu bind:this={treeContextMenu} oncreate={() => void rename.createAndRename()} {oncreatesmartlist} onrename={(node) => void rename.begin(node)} deleteNode={ondeleteplaylist} onduplicate={onduplicateplaylist} onforbidduplicates={onforbidduplicates} {onselect} />
	{#if columnMounted}
		<div
			class="tree-scroll column-mode"
			class:hidden={mode !== 'column'}
			data-testid="playlist-column-view"
		>
			<ColumnBrowser selectedId={trackSelectedId} {onselecttrack} {onloadtrack} />
		</div>
	{/if}
	<div
		class="tree-scroll"
		class:hidden={mode === 'column'}
		class:has-fold={foldTracker.current !== null}
		class:folder-drop-target={folderDropActive}
		bind:this={foldTracker.scrollEl}
		bind:clientHeight={foldTracker.viewportHeight}
		onscroll={foldTracker.onScroll}
		ondragover={_onFolderDragOver}
		ondragleave={_onFolderDragLeave}
		ondrop={_onFolderDrop}
	>
		<TreeCurrentFold fold={foldTracker.current} onjump={() => foldTracker.jumpToCurrent()} />
		<div
			class="row"
			class:selected={selectedId === 'all'}
			data-testid="playlist-all-tracks"
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
			<span class="count" title={_allTracksCountTitle()}>{allTracksError === null ? allTracksCount ?? '...' : '!'}</span>
		</div>
		<div
			class="row folder"
			data-testid="playlist-folder"
			role="button"
			tabindex="0"
			onclick={() => (playlistsOpen = !playlistsOpen)}
			onkeydown={(e) => {
				if (e.key === 'Enter') playlistsOpen = !playlistsOpen;
				treeContextMenu?.openFromKeyboard(e, 'folder');
			}}
			oncontextmenu={(e) => treeContextMenu?.open(e, 'folder')}
		>
			<span class="disclosure" class:open={playlistsOpen}>&#9656;</span>
			<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
				<path d="M1 3h5l1.5 2H15v8H1z" fill="currentColor" />
			</svg>
			<span class="name">Playlists</span>
			{#if oncreateplaylist}
				<button
					type="button"
					class="pl-action"
					title="Create playlist"
					onclick={(e) => {
						e.stopPropagation();
						void rename.createAndRename();
					}}
				>
					+
				</button>
			{/if}
		</div>
		{#if playlistsOpen}
			<PlaylistFolderStates
				nodeCount={nodes.length}
				{playlistsLoading}
				{playlistsError}
				{hiddenBrokenPlaylistCount}
				oncreate={oncreateplaylist ? () => void rename.createAndRename() : undefined}
			/>
			{#each nodes as node (node.playlist_id)}
				<div
					class="row child"
					data-testid="playlist-row"
					class:selected={selectedId === node.playlist_id}
					class:broken={node.mostly_broken}
					title={node.mostly_broken ? formatMostlyBrokenTooltip() : undefined}
					class:drop-target={dropTargetId === node.playlist_id}
					class:tint-deck={_tintOf(node) === 'deck'}
					class:tint-multi={_tintOf(node) === 'multi'}
					role="button"
					tabindex="0"
					draggable="true"
					use:foldTracker.bindSelectedRow={selectedId === node.playlist_id}
					onclick={() => onselect(node)}
					onkeydown={(e) => { _rowKeydown(e, node); treeContextMenu?.openFromKeyboard(e, 'playlist', node); }}
					oncontextmenu={(e) => treeContextMenu?.open(e, 'playlist', node)}
					ondragstart={(e) => _onPlaylistDragStart(e, node)}
					ondragover={(e) => _onTrackDragOver(e, node)}
					ondragleave={() => _onTrackDragLeave(node)}
					ondrop={(e) => _onTrackDrop(e, node)}
				>
					<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
						<path d="M2 3h8v2H2zM2 7h8v2H2zM2 11h8v2H2zM11 5l4 3-4 3z" fill="currentColor" />
					</svg>
					{#if rename.editingId === node.playlist_id}
						<input
							bind:this={rename.inputEl}
							class="rename-input"
							type="text"
							value={rename.editDraft}
							aria-label="Rename playlist"
							onclick={(e) => e.stopPropagation()}
							onmousedown={(e) => e.stopPropagation()}
							oninput={(e) => (rename.editDraft = e.currentTarget.value)}
							onkeydown={(e) => rename.onKeydown(e)}
							onblur={() => void rename.commit()}
						/>
					{:else}
						<span class="name" title={node.mostly_broken ? formatMostlyBrokenTooltip() : node.name}>{node.name}</span>
					{/if}
					{#if onrenameplaylist}
						<button
							type="button"
							class="pl-action dim"
							title="Rename playlist"
							aria-label="Rename playlist"
							onclick={(e) => {
								e.stopPropagation();
								void rename.begin(node);
							}}
						>
							<svg viewBox="0 0 24 24" width="11" height="11" aria-hidden="true">
								<path d={PENCIL_PATH} fill="none" stroke="currentColor" stroke-width="2" />
							</svg>
						</button>
					{/if}
					{#if ondeleteplaylist}
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
				{#if rename.createdId === node.playlist_id && node.track_count === 0}
					<div class="row child hint" data-testid="playlist-new-hint">
						Drag tracks or a folder here to add music
					</div>
				{/if}
			{/each}
			<PlaylistHiddenBrokenNotice {hiddenBrokenPlaylistCount} />
		{/if}
		<RecentlyDeletedFolder />
		<!-- data-testid="playlist-missing-tracks" is on MissingTracksFolder -->
		<MissingTracksFolder
			brokenCount={allTracksBrokenCount}
			error={allTracksError}
			selected={selectedId === MISSING_TRACKS_ID}
			onselect={() => onselect(missingTracksNode(allTracksBrokenCount ?? 0))}
		/>
	</div>
</div>

<style>
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
		color: var(--rb-text);
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
