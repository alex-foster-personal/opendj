<script lang="ts">
	import type { DeckId } from '$lib/rb/deck-slots';
	import type { PlaylistNode } from '$lib/rb/library-types';
	import { type SmartlistSummary } from '$lib/rb/api-smartlists';
	import type { ColumnTrackRow } from './ColumnBrowser.svelte';
	import LibrarySourceTabs, { type LibrarySourceTab } from './LibrarySourceTabs.svelte';
	import PlaylistTree from './PlaylistTree.svelte';
	import TaglistTree from './TaglistTree.svelte';
	import TreeSmartlistSection from './TreeSmartlistSection.svelte';
	import TreeContextMenu from './TreeContextMenu.svelte';
	import UsbSourceList from './UsbSourceList.svelte';

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
		playlistsLoading: boolean;
		playlistsError: string | null;
		allTracksCount: number | null;
		allTracksBrokenCount: number | null;
		allTracksError: string | null;
		selectedId: string | null;
		trackSelectedId: string | null;
		deckLoadedPlaylistIds?: ReadonlySet<string>;
		multiPanePlaylistIds?: ReadonlySet<string>;
		onselect: (node: PlaylistNode) => void;
		onselectsmartlist?: (smartlist: SmartlistSummary) => void;
		onselecttrack?: (row: ColumnTrackRow) => void;
		onloadtrack?: (row: ColumnTrackRow, deck: DeckId | null) => void;
		oncreateplaylist?: () => Promise<string | null> | string | null;
		onrenameplaylist?: (node: PlaylistNode, name: string) => void | Promise<void>;
		ondeleteplaylist?: (node: PlaylistNode) => void;
		onduplicateplaylist?: (node: PlaylistNode) => void;
		ondroptracks?: (playlistId: string, stableIds: string[]) => void;
	} = $props();

	let activeTab = $state<LibrarySourceTab>('playlists');
	let treeContextMenu = $state<TreeContextMenu | null>(null);
	let deleteSmartlistUi: ((sl: { id: string; name: string }) => void) | undefined;
</script>

<div class="library-nav-root">
	<LibrarySourceTabs active={activeTab} onchange={(tab) => (activeTab = tab)} />
	{#if activeTab === 'playlists'}
		<PlaylistTree
			{nodes}
			{playlistsLoading}
			{playlistsError}
			{allTracksCount}
			{allTracksBrokenCount}
			{allTracksError}
			{selectedId}
			{trackSelectedId}
			{deckLoadedPlaylistIds}
			{multiPanePlaylistIds}
			{onselect}
			{onselectsmartlist}
			{onselecttrack}
			{onloadtrack}
			{oncreateplaylist}
			{onrenameplaylist}
			{ondeleteplaylist}
			{onduplicateplaylist}
			{ondroptracks}
		/>
	{:else if activeTab === 'taglists'}
		<TaglistTree {selectedId} {onselect} />
	{:else if activeTab === 'autolists'}
		<TreeContextMenu
			bind:this={treeContextMenu}
			onselect={() => {}}
			ondeletesmartlist={(sl) => deleteSmartlistUi?.(sl)}
		/>
		<div class="autolists-scroll">
			<TreeSmartlistSection
				{selectedId}
				{onselectsmartlist}
				{treeContextMenu}
				onDeleteReady={(fn) => {
					deleteSmartlistUi = fn;
				}}
			/>
		</div>
	{:else}
		<UsbSourceList />
	{/if}
</div>

<style>
	.library-nav-root {
		display: flex;
		flex-direction: column;
		min-height: 0;
		height: 100%;
	}
	.autolists-scroll {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		padding: 2px 0;
	}
</style>
