<script lang="ts">
	// Playlist tree panel (SCREENSHOT-SPEC 5b). 'All Tracks' shows OUR live
	// total (never the screenshot's 9862); 'Playlists' folder lists real
	// playlists with right-aligned counts in rekordbox custom tree order
	// (djmdPlaylist Seq, sorted upstream in BrowserPanel - COMPONENT-MAP
	// 1.5). The Column View tab (column-view lane) swaps this panel's body
	// for ColumnBrowser - self-contained (own library fetch), same pattern
	// as the smartlist self-fetch below.
	import type { DeckId } from '$lib/rb/deck-slots';
	import type { PlaylistNode } from '$lib/rb/library-types';
	import { type SmartlistSummary } from '$lib/rb/api-smartlists';
	import ColumnBrowser, { type ColumnTrackRow } from './ColumnBrowser.svelte';
	import {
		acceptTrackDragOver,
		droppedStableIds,
		endTrackDrag
	} from '$lib/rb/track-drag.svelte';
	import { encodePlaylistDrag, PLAYLIST_DRAG_MIME } from './playlist-drag';
	import { type PlaylistTint, playlistTintOf } from './pane-contract.svelte';
	import TreeCurrentFold from './TreeCurrentFold.svelte';
	import { TreeFoldTracker } from './tree-fold-tracker.svelte';
	import { TreeSmartlists } from './tree-smartlists.svelte';
	import { TreePlaylistRename } from './tree-playlist-rename.svelte';
	import TreeContextMenu from './TreeContextMenu.svelte';
	import MissingTracksFolder from './MissingTracksFolder.svelte';
	import { MISSING_TRACKS_ID, missingTracksNode } from './missing-tracks';
	import PlaylistHistoryPanel from './PlaylistHistoryPanel.svelte';

	let {
		nodes,
		playlistsLoading,
		playlistsError,
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
		ondeleteplaylist,
		onduplicateplaylist,
		ondroptracks
	}: {
		nodes: PlaylistNode[];
		/** Pin e0f3a90652a9: names/counts is a cheap read, but available_count
		 * is a real per-member disk-existence pass and can take a couple of
		 * seconds cold (first load after app focus). Required, not optional --
		 * an unwired caller must fail the Svelte type check rather than silently
		 * render as if already loaded (a missed wire previously read as a
		 * healthy empty playlist panel, the exact silent-success failure the
		 * pin was about). */
		playlistsLoading: boolean;
		playlistsError: string | null;
		allTracksCount: number | null;
		allTracksBrokenCount: number | null;
		allTracksError: string | null;
		selectedId: string | null;
		/** The active pane's PaneStore.selected_id, forwarded to ColumnBrowser
		 * so its track highlight reflects the current pane rather than an
		 * independent selection that would desync on pane switches. Distinct
		 * from `selectedId` above, which is the selected PLAYLIST id. */
		trackSelectedId: string | null;
		/** Pin 2ac3a0: playlists holding a track currently loaded into a deck
		 * (light blue tint) and playlists open in 2+ pane tabs at once
		 * (darker blue tint) - both derived by BrowserPanel from ALREADY
		 * HYDRATED pane rows only. Absent = no tinting (e.g. an integrator
		 * that hasn't wired deck/pane context yet). */
		deckLoadedPlaylistIds?: ReadonlySet<string>;
		multiPanePlaylistIds?: ReadonlySet<string>;
		onselect: (node: PlaylistNode) => void;
		/** Optional until the browser integrator wires smartlist selection
		 * into BrowserPanel; absent = smartlist rows render inert. */
		onselectsmartlist?: (smartlist: SmartlistSummary) => void;
		/** Column View lane callbacks - optional, same absent-means-inert
		 * convention as onselectsmartlist (ColumnBrowser itself no-ops a
		 * click/dblclick with no handler wired). */
		onselecttrack?: (row: ColumnTrackRow) => void;
		onloadtrack?: (row: ColumnTrackRow, deck: DeckId | null) => void;
		/** Create then return new playlist_id (or null on cancel/fail). */
		oncreateplaylist?: () => Promise<string | null> | string | null;
		/** Commit in-place rename; empty/cancelled name leaves server name. */
		onrenameplaylist?: (node: PlaylistNode, name: string) => void | Promise<void>;
		ondeleteplaylist?: (node: PlaylistNode) => void;
		onduplicateplaylist?: (node: PlaylistNode) => void;
		/**
		 * Library tracks dropped onto a playlist row. Absent = rows are not
		 * drop targets, same absent-means-inert convention as above.
		 */
		ondroptracks?: (playlistId: string, stableIds: string[]) => void;
	} = $props();

	/** playlist_id currently under a track drag, for the drop outline. */
	let dropTargetId: string | null = $state(null);
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

	/** Make a playlist row draggable onto the pane tab bar. */
	function _onPlaylistDragStart(event: DragEvent, node: PlaylistNode): void {
		if (node.kind === 'missing_tracks') return;
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

	let mode = $state<'tree' | 'column'>('tree');
	// ColumnBrowser mounts lazily on first activation (its onMount walks
	// every /tracks cursor page - no point paying that for users who never
	// open Column View) but then STAYS mounted (visibility toggled via CSS
	// below, not {#if}/{:else}) so switching back to Tree View and back
	// doesn't re-trigger the full-library fetch every time.
	let columnMounted = $state(false);
	$effect(() => {
		if (mode === 'column') columnMounted = true;
	});
	let playlistsOpen = $state(true);
	/** Smartlists tree-section state (tree-smartlists.svelte.ts). */
	const smartlists = new TreeSmartlists(() => onselectsmartlist);

	const allNode = $derived<PlaylistNode>({
		playlist_id: 'all',
		name: 'All Tracks',
		track_count: allTracksCount ?? 0,
		broken_count: allTracksBrokenCount ?? 0,
		kind: 'all_tracks',
		children: []
	});

	function _allTracksCountTitle(): string {
		if (allTracksError !== null) return `playable count unavailable: ${allTracksError}`;
		if (allTracksCount === null || allTracksBrokenCount === null) return 'loading playable and broken track counts';
		return `${allTracksCount} playable tracks, ${allTracksBrokenCount} broken tracks`;
	}

	function _playlistCountTitle(node: PlaylistNode): string {
		return `${node.track_count - node.broken_count} playable tracks, ${node.broken_count} broken tracks`;
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

<div class="tree-root">
	<PlaylistHistoryPanel />
	<TreeContextMenu bind:this={treeContextMenu} oncreate={() => void rename.createAndRename()} onrename={(node) => void rename.begin(node)} deleteNode={ondeleteplaylist} onduplicate={onduplicateplaylist} {onselect} />
	<div class="view-tabs">
		<button class="vt" class:active={mode === 'tree'} onclick={() => (mode = 'tree')}>
			Tree View
		</button>
		<button class="vt" class:active={mode === 'column'} onclick={() => (mode = 'column')}>
			Column View
		</button>
	</div>
	{#if columnMounted}
		<div class="tree-scroll column-mode" class:hidden={mode !== 'column'}>
			<ColumnBrowser selectedId={trackSelectedId} {onselecttrack} {onloadtrack} />
		</div>
	{/if}
	<TreeCurrentFold fold={foldTracker.current} onjump={() => foldTracker.jumpToCurrent()} />
	<div
		class="tree-scroll"
		class:hidden={mode === 'column'}
		bind:this={foldTracker.scrollEl}
		bind:clientHeight={foldTracker.viewportHeight}
		onscroll={foldTracker.onScroll}
	>
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
			{#if nodes.length === 0 && playlistsLoading}
				<div class="row child rb-inert" data-testid="playlists-loading">Loading playlists...</div>
			{:else if nodes.length === 0 && playlistsError !== null}
				<div class="row child rb-inert" title={playlistsError}>Playlist load failed</div>
			{/if}
			{#each nodes as node (node.playlist_id)}
				<div
					class="row child"
					data-testid="playlist-row"
					class:selected={selectedId === node.playlist_id}
					class:broken={node.mostly_broken}
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
						<span class="name" title={node.name}>{node.name}</span>
					{/if}
					{#if onrenameplaylist}
						<button
							type="button"
							class="pl-action dim"
							title="Rename playlist"
							onclick={(e) => {
								e.stopPropagation();
								void rename.begin(node);
							}}
						>
							✎
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
			{/each}
		{/if}
		<div
			class="row folder"
			role="button"
			tabindex="0"
			onclick={() => smartlists.toggle()}
			onkeydown={(e) => {
				if (e.key === 'Enter') smartlists.toggle();
			}}
		>
			<span class="disclosure" class:open={smartlists.open}>&#9656;</span>
			<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
				<path d="M1 3h5l1.5 2H15v8H1z" fill="currentColor" />
			</svg>
			<span class="name">Smartlists</span>
		</div>
		{#if smartlists.open}
			{#if smartlists.error !== null}
				<div class="row child rb-inert" title={smartlists.error}>
					<span class="name error">smartlists unavailable</span>
				</div>
			{:else if smartlists.rows === null}
				<div class="row child rb-inert">
					<span class="name dim">...</span>
				</div>
			{:else}
				{#each smartlists.rows as sl (sl.id)}
					<div
						class="row child"
						class:rb-inert={!onselectsmartlist}
						class:selected={selectedId === sl.id}
						role="button"
						tabindex="0"
						title={onselectsmartlist
							? sl.rule_summary
							: 'not implemented - see PARITY-TODO'}
						onclick={() => smartlists.click(sl)}
						onkeydown={(e) => {
							if (e.key === 'Enter') smartlists.click(sl);
						}}
					>
						<svg class="gear" viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
							<path
								d="M8 5.2A2.8 2.8 0 1 0 8 10.8 2.8 2.8 0 0 0 8 5.2zm0 4.3a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zm6-.5.1-1-1.5-.6-.2-.6.8-1.4-.7-.7-1.4.8-.6-.3L9.9 3.7h-1L8.3 5.2l-.6.3-1.4-.8-.7.7.8 1.4-.3.6-1.5.5v1l1.5.6.3.6-.8 1.4.7.7 1.4-.8.6.3.6 1.5h1l.6-1.5.6-.3 1.4.8.7-.7-.8-1.4.3-.6z"
								fill="currentColor"
							/>
						</svg>
						<span class="name" title={sl.rule_summary}>{sl.name}</span>
					</div>
				{/each}
			{/if}
		{/if}
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
