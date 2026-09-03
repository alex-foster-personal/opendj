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
	import { listSmartlists, type SmartlistSummary } from '$lib/rb/api-smartlists';
	import { RbApiError } from '$lib/rb/api-rb';
	import { tick } from 'svelte';
	import ColumnBrowser, { type ColumnTrackRow } from './ColumnBrowser.svelte';
	import {
		acceptTrackDragOver,
		droppedStableIds,
		endTrackDrag
	} from '$lib/rb/track-drag.svelte';
	import { encodePlaylistDrag, PLAYLIST_DRAG_MIME } from './pane-contract.svelte';

	let {
		nodes,
		allTracksCount,
		allTracksBrokenCount,
		allTracksError,
		selectedId,
		trackSelectedId,
		onselect,
		onselectsmartlist,
		onselecttrack,
		onloadtrack,
		oncreateplaylist,
		onrenameplaylist,
		ondeleteplaylist,
		ondroptracks
	}: {
		nodes: PlaylistNode[];
		allTracksCount: number | null;
		allTracksBrokenCount: number | null;
		allTracksError: string | null;
		selectedId: string | null;
		/** The active pane's PaneStore.selected_id, forwarded to ColumnBrowser
		 * so its track highlight reflects the current pane rather than an
		 * independent selection that would desync on pane switches. Distinct
		 * from `selectedId` above, which is the selected PLAYLIST id. */
		trackSelectedId: string | null;
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
		/**
		 * Library tracks dropped onto a playlist row. Absent = rows are not
		 * drop targets, same absent-means-inert convention as above.
		 */
		ondroptracks?: (playlistId: string, stableIds: string[]) => void;
	} = $props();

	/** playlist_id currently under a track drag, for the drop outline. */
	let dropTargetId: string | null = $state(null);

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

	let editingId = $state<string | null>(null);
	let editDraft = $state('');
	let renameInputEl = $state<HTMLInputElement | null>(null);
	/** Set after '+'; rename starts once the new node appears in `nodes`. */
	let pendingRenameId = $state<string | null>(null);

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
	let smartlistsOpen = $state(true);
	// Smartlists are self-fetched here (LANE smartlists-router) so this
	// component needs zero BrowserPanel / api-rb.ts (hotspot) changes.
	let smartlists = $state<SmartlistSummary[] | null>(null);
	let smartlistsError = $state<string | null>(null);

	$effect(() => {
		listSmartlists().then(
			(rows) => (smartlists = rows),
			(err: unknown) => {
				// Explicit backend error (e.g. SMARTLISTS_DB_UNAVAILABLE on an
				// in-memory deploy) renders as a dim error row - never hidden.
				smartlistsError = err instanceof RbApiError ? err.code : String(err);
			}
		);
	});

	function _smartlistClick(sl: SmartlistSummary): void {
		if (onselectsmartlist) onselectsmartlist(sl);
	}

	/** Static chrome per SCREENSHOT-SPEC 5b: the CUE Analysis Playlist row
	 * carries an "extra" badge in the reference screenshot; not tied to any
	 * real analysis state. */
	function _hasExtraBadge(name: string): boolean {
		return name === 'CUE Analysis Playlist';
	}

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

	async function _beginRename(node: PlaylistNode): Promise<void> {
		if (onrenameplaylist === undefined) return;
		if (node.kind === 'all_tracks' || node.playlist_id === 'all') return;
		editingId = node.playlist_id;
		editDraft = node.name;
		await tick();
		renameInputEl?.focus();
		renameInputEl?.select();
	}

	async function _commitRename(): Promise<void> {
		const id = editingId;
		if (id === null || onrenameplaylist === undefined) return;
		const node = nodes.find((n) => n.playlist_id === id);
		editingId = null;
		if (node === undefined) return;
		const next = editDraft.trim();
		if (next === '' || next === node.name) return;
		await onrenameplaylist(node, next);
	}

	function _cancelRename(): void {
		editingId = null;
	}

	async function _createAndRename(): Promise<void> {
		if (oncreateplaylist === undefined) return;
		const createdId = await oncreateplaylist();
		if (createdId === null || createdId === '') return;
		pendingRenameId = createdId;
	}

	function _renameKeydown(event: KeyboardEvent): void {
		if (event.key === 'Enter') {
			event.preventDefault();
			void _commitRename();
		} else if (event.key === 'Escape') {
			event.preventDefault();
			_cancelRename();
		}
	}

	$effect(() => {
		const id = pendingRenameId;
		if (id === null) return;
		const node = nodes.find((n) => n.playlist_id === id);
		if (node === undefined) return;
		pendingRenameId = null;
		void _beginRename(node);
	});
</script>

<div class="tree-root">
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
	<div class="tree-scroll" class:hidden={mode === 'column'}>
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
			{#if oncreateplaylist}
				<button
					type="button"
					class="pl-action"
					title="Create playlist"
					onclick={(e) => {
						e.stopPropagation();
						void _createAndRename();
					}}
				>
					+
				</button>
			{/if}
		</div>
		{#if playlistsOpen}
			{#each nodes as node (node.playlist_id)}
				<div
					class="row child"
					class:selected={selectedId === node.playlist_id}
					class:broken={node.mostly_broken}
					class:drop-target={dropTargetId === node.playlist_id}
					role="button"
					tabindex="0"
					draggable="true"
					onclick={() => onselect(node)}
					onkeydown={(e) => _rowKeydown(e, node)}
					ondragstart={(e) => _onPlaylistDragStart(e, node)}
					ondragover={(e) => _onTrackDragOver(e, node)}
					ondragleave={() => _onTrackDragLeave(node)}
					ondrop={(e) => _onTrackDrop(e, node)}
				>
					<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
						<path d="M2 3h8v2H2zM2 7h8v2H2zM2 11h8v2H2zM11 5l4 3-4 3z" fill="currentColor" />
					</svg>
					{#if editingId === node.playlist_id}
						<input
							bind:this={renameInputEl}
							class="rename-input"
							type="text"
							value={editDraft}
							aria-label="Rename playlist"
							onclick={(e) => e.stopPropagation()}
							onmousedown={(e) => e.stopPropagation()}
							oninput={(e) => (editDraft = e.currentTarget.value)}
							onkeydown={_renameKeydown}
							onblur={() => void _commitRename()}
						/>
					{:else}
						<span class="name" title={node.name}>{node.name}</span>
					{/if}
					<span class="count" title={_playlistCountTitle(node)}>{node.track_count - node.broken_count}</span>
					{#if onrenameplaylist}
						<button
							type="button"
							class="pl-action dim"
							title="Rename playlist"
							onclick={(e) => {
								e.stopPropagation();
								void _beginRename(node);
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
					{#if _hasExtraBadge(node.name)}
						<span class="badge badge-extra" aria-hidden="true">extra</span>
					{/if}
					{#if selectedId === node.playlist_id}
						<span class="badge badge-plus" aria-hidden="true">+</span>
					{/if}
				</div>
			{/each}
		{/if}
		<div
			class="row folder"
			role="button"
			tabindex="0"
			onclick={() => (smartlistsOpen = !smartlistsOpen)}
			onkeydown={(e) => {
				if (e.key === 'Enter') smartlistsOpen = !smartlistsOpen;
			}}
		>
			<span class="disclosure" class:open={smartlistsOpen}>&#9656;</span>
			<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
				<path d="M1 3h5l1.5 2H15v8H1z" fill="currentColor" />
			</svg>
			<span class="name">Smartlists</span>
		</div>
		{#if smartlistsOpen}
			{#if smartlistsError !== null}
				<div class="row child rb-inert" title={smartlistsError}>
					<span class="name error">smartlists unavailable</span>
				</div>
			{:else if smartlists === null}
				<div class="row child rb-inert">
					<span class="name dim">...</span>
				</div>
			{:else}
				{#each smartlists as sl (sl.id)}
					<div
						class="row child"
						class:rb-inert={!onselectsmartlist}
						class:selected={selectedId === sl.id}
						role="button"
						tabindex="0"
						title={onselectsmartlist
							? sl.rule_summary
							: 'not implemented - see PARITY-TODO'}
						onclick={() => _smartlistClick(sl)}
						onkeydown={(e) => {
							if (e.key === 'Enter') _smartlistClick(sl);
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
	.badge-extra {
		color: var(--rb-text-dim);
		border: 1px solid var(--rb-border);
		background: var(--rb-panel-raised);
		text-transform: uppercase;
		letter-spacing: 0.04em;
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
