<script lang="ts">
	// Playlist tree panel (SCREENSHOT-SPEC 5b). 'All Tracks' shows OUR live
	// total (never the screenshot's 9862); 'Playlists' folder lists real
	// playlists with right-aligned counts in rekordbox custom tree order
	// (djmdPlaylist Seq, sorted upstream in BrowserPanel - COMPONENT-MAP
	// 1.5). The Column View tab (column-view lane) swaps this panel's body
	// for ColumnBrowser - self-contained (own library fetch), same pattern
	// as the smartlist self-fetch below.
	import { tick } from 'svelte';
	import type { PlaylistNode } from '$lib/rb/library-types';
	import { playlistReveal } from '$lib/rb/playlist-reveal.svelte';
	import { formatMostlyBrokenTooltip } from '$lib/rb/runtime-policy.svelte';
	import ColumnBrowser from './ColumnBrowser.svelte';
	import type { PlaylistTreeProps } from './playlist-tree-props';
	import {
		acceptTrackDragOver,
		droppedStableIds,
		endTrackDrag,
		refuseTrackDragOver,
		trackDropRefusal
	} from '$lib/rb/track-drag.svelte';
	import { encodePlaylistDrag, PLAYLIST_DRAG_MIME } from './playlist-drag';
	import { dropPlaceAt, type DropPlace } from './playlist-tree-order';
	import { isInboxPlaylistId } from './inbox-id';
	import { trackDrag } from '$lib/rb/track-drag.svelte';
	import { isOsFileDrag } from '$lib/rb/ingest-drop-files';
	import { type PlaylistTint, playlistTintOf } from './pane-contract.svelte';
	import { TreeFoldTracker } from './tree-fold-tracker.svelte';
	import { TreePlaylistRename } from './tree-playlist-rename.svelte';
	import TreeContextMenu from './TreeContextMenu.svelte';
	import RecentlyDeletedFolder from './RecentlyDeletedFolder.svelte';
	import { missingTracksNode } from './missing-tracks';
	import PlaylistTreeView from './PlaylistTreeView.svelte';
	import { sidebarArtShown } from '$lib/rb/library-sidebar-art.svelte';
	import type { PlaylistTreeViewMode } from '$lib/rb/playlist-tree-view-prefs';
	import { prefetchPlaylistFirstPage } from '$lib/rb/library-playlist-page-prefetch';
	import {
		playlistTreeSearch,
		playlistTreeSearchView,
		setPlaylistTreeSearch
	} from '$lib/rb/playlist-tree-search.svelte';
	import {
		ensurePlaylistTreePinsLoaded,
		pinnedShortcutNodes,
		playlistTreePins,
		setPlaylistPinned
	} from '$lib/rb/playlist-tree-pins.svelte';
	import { pushToast } from '$lib/stores.svelte';
	let {
		nodes,
		mode = 'tree',
		searchable = true,
		playlistsLoading,
		playlistsError,
		hiddenBrokenPlaylistCount,
		allTracksCount,
		allTracksBrokenCount,
		allTracksError,
		allTracksAvailability,
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
		onfolderdrop,
		onmoveplaylist,
		onsortplaylists
	}: PlaylistTreeProps & {
		mode?: PlaylistTreeViewMode;
		/** LIBM-87: draw the tree's own name search; off where a host already searches it. */
		searchable?: boolean;
	} = $props();

	/** playlist_id currently under a track drag, for the drop outline. */
	let dropTargetId: string | null = $state(null);
	/** LIBM-84: the smartlist row a track drag is over, refused out loud. */
	let refusedDropId: string | null = $state(null);
	let folderDropActive = $state(false);
	let treeContextMenu = $state<TreeContextMenu | null>(null);
	/** LIBTREE-01: the playlist row being dragged to a new place in the tree,
	 * and where it would land. Null while no playlist drag is in flight. */
	let movingPlaylistId: string | null = $state(null);
	let reorderTarget: { id: string; place: DropPlace } | null = $state(null);

	function _onReorderDragOver(event: DragEvent, node: PlaylistNode): boolean {
		// A track drag never reorders; a playlist drag whose dragend was lost
		// must not keep track drops dead either.
		if (trackDrag.active) movingPlaylistId = null;
		if (movingPlaylistId === null || node.kind !== 'playlist') return false;
		event.preventDefault();
		if (event.dataTransfer !== null) event.dataTransfer.dropEffect = 'move';
		const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
		reorderTarget =
			node.playlist_id === movingPlaylistId
				? null
				: { id: node.playlist_id, place: dropPlaceAt(event.clientY, rect.top, rect.height) };
		return true;
	}

	function _onReorderDrop(event: DragEvent, node: PlaylistNode): boolean {
		if (movingPlaylistId === null) return false;
		event.preventDefault();
		const moved = movingPlaylistId;
		const target = reorderTarget;
		_endReorderDrag();
		if (target !== null && target.id === node.playlist_id) onmoveplaylist?.(moved, target.id, target.place);
		return true;
	}

	function _endReorderDrag(): void {
		movingPlaylistId = null;
		reorderTarget = null;
	}

	function _onTrackDragOver(event: DragEvent, node: PlaylistNode): void {
		if (_onReorderDragOver(event, node)) return;
		// LIBM-84: a smartlist fills from its rules; refuse before any request.
		if (trackDropRefusal(node.kind) !== null) {
			if (refuseTrackDragOver(event)) refusedDropId = node.playlist_id;
			return;
		}
		// All Tracks is a view, not a playlist, so it can never receive a drop.
		if (ondroptracks === undefined || node.kind !== 'playlist' || isInboxPlaylistId(node.playlist_id))
			return;
		// Same WKWebView rule as the deck drop targets: accept on the in-app
		// drag state, never on dataTransfer.types (see track-drag.svelte.ts).
		if (!acceptTrackDragOver(event)) return;
		dropTargetId = node.playlist_id;
	}

	function _onTrackDragLeave(node: PlaylistNode): void {
		if (reorderTarget?.id === node.playlist_id) reorderTarget = null;
		if (dropTargetId === node.playlist_id) dropTargetId = null;
		if (refusedDropId === node.playlist_id) refusedDropId = null;
	}

	function _onTrackDrop(event: DragEvent, node: PlaylistNode): void {
		if (_onReorderDrop(event, node)) return;
		refusedDropId = null;
		if (ondroptracks === undefined || node.kind !== 'playlist' || isInboxPlaylistId(node.playlist_id))
			return;
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
			node.kind === 'usb' ||
			node.kind === 'history' ||
			node.kind === 'tag_list' ||
			node.kind === 'explorer'
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
		// A library playlist may also move within the tree (LIBTREE-01); the
		// pane tab bar still takes the drag as a copy.
		const reorderable =
			node.kind === 'playlist' && !isInboxPlaylistId(node.playlist_id) && onmoveplaylist !== undefined;
		if (reorderable) movingPlaylistId = node.playlist_id;
		if (event.dataTransfer !== null) event.dataTransfer.effectAllowed = reorderable ? 'copyMove' : 'copy';
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

	const allNode = $derived<PlaylistNode>({
		playlist_id: 'all',
		name: 'All Tracks',
		track_count: allTracksCount ?? 0,
		broken_count: allTracksBrokenCount ?? 0,
		kind: 'all_tracks',
		children: []
	});

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
	let playlistsOpen = $state(true);
	/** LIBM-87: the rows, open state and empty state the tree's search shows. */
	const treeSearch = $derived(
		playlistTreeSearchView(nodes, searchable ? playlistTreeSearch.query : '', playlistsOpen)
	);

	/** LIBM-87 (issue #12320): pinned shortcuts above the Playlists section; hidden while searching. */
	$effect(() => {
		ensurePlaylistTreePinsLoaded();
	});
	const pinnedIds = $derived(new Set(playlistTreePins.ids));
	const pinnedNodes = $derived(
		!treeSearch.active ? pinnedShortcutNodes(nodes, playlistTreePins.ids) : []
	);

	function _togglePin(node: PlaylistNode): void {
		const pinned = !pinnedIds.has(node.playlist_id);
		setPlaylistPinned(node.playlist_id, pinned).catch((exc: unknown) =>
			pushToast(`${pinned ? 'pin' : 'unpin'} "${node.name}" failed: ${String(exc)}`, 'error')
		);
	}

	/** CMDK-6848: open the Playlists section, bring the asked row into view, and clear the request. */
	$effect(() => {
		const id = playlistReveal.id;
		if (id === null) return;
		playlistReveal.id = null;
		playlistsOpen = true;
		void tick().then(() =>
			foldTracker.scrollEl
				?.querySelector(`[data-playlist-id="${CSS.escape(id)}"]`)
				?.scrollIntoView({ block: 'nearest' })
		);
	});
</script>

{#snippet contextMenu()}
	<TreeContextMenu bind:this={treeContextMenu} oncreate={() => void rename.createAndRename()} {oncreatesmartlist} onrename={(node) => void rename.begin(node)} deleteNode={ondeleteplaylist} onduplicate={onduplicateplaylist} onforbidduplicates={onforbidduplicates} {onselect} {onsortplaylists} />
{/snippet}

{#snippet columnBrowser()}
	<ColumnBrowser selectedId={trackSelectedId} {onselecttrack} {onloadtrack} />
{/snippet}

{#snippet recentlyDeleted()}
	<RecentlyDeletedFolder />
{/snippet}

<PlaylistTreeView
	nodes={treeSearch.nodes}
	unfilteredCount={nodes.length}
	{mode}
	{searchable}
	searchQuery={playlistTreeSearch.query}
	searchNoMatches={treeSearch.noMatches}
	listOpen={treeSearch.open}
	onsearch={setPlaylistTreeSearch}
	{pinnedNodes}
	{pinnedIds}
	ontogglepin={_togglePin}
	{playlistsLoading}
	{playlistsError}
	{hiddenBrokenPlaylistCount}
	{allTracksCount}
	{allTracksBrokenCount}
	{allTracksError}
	{allTracksAvailability}
	{allNode}
	{selectedId}
	mostlyBrokenTitle={formatMostlyBrokenTooltip()}
	tintOf={_tintOf}
	{columnMounted}
	{dropTargetId}
	refusedDropId={trackDrag.active ? refusedDropId : null}
	{folderDropActive}
	bind:playlistsOpen
	foldCurrent={foldTracker.current}
	bind:scrollEl={foldTracker.scrollEl}
	bind:viewportHeight={foldTracker.viewportHeight}
	selectedRowAction={foldTracker.bindSelectedRow}
	{movingPlaylistId}
	{reorderTarget}
	onrowdragend={_endReorderDrag}
	editingId={rename.editingId}
	editDraft={rename.editDraft}
	createdId={rename.createdId}
	bind:renameInputEl={rename.inputEl}
	cancreate={oncreateplaylist !== undefined}
	canrename={onrenameplaylist !== undefined}
	sidebarart={sidebarArtShown()}
	{ondeleteplaylist}
	{onselect}
	onselectmissing={() => onselect(missingTracksNode(allTracksBrokenCount ?? 0))}
	oncreate={() => void rename.createAndRename()}
	onbeginrename={(node) => void rename.begin(node)}
	oneditdraft={(value) => (rename.editDraft = value)}
	onrenamekeydown={(e) => rename.onKeydown(e)}
	onrenamecommit={() => void rename.commit()}
	onmenu={(e, kind, node) => treeContextMenu?.open(e, kind, node)}
	onmenukey={(e, kind, node) => treeContextMenu?.openFromKeyboard(e, kind, node)}
	onprefetch={prefetchPlaylistFirstPage}
	onscroll={foldTracker.onScroll}
	onjump={() => foldTracker.jumpToCurrent()}
	onfolderdragover={_onFolderDragOver}
	onfolderdragleave={_onFolderDragLeave}
	onfolderdrop={_onFolderDrop}
	onrowdragstart={_onPlaylistDragStart}
	onrowdragover={_onTrackDragOver}
	onrowdragleave={_onTrackDragLeave}
	onrowdrop={_onTrackDrop}
	{contextMenu}
	{columnBrowser}
	{recentlyDeleted}
/>
