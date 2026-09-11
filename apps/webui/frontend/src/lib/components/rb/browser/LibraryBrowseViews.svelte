<script lang="ts">
	import type { DeckId } from '$lib/rb/deck-slots';
	import type { PlaylistNode } from '$lib/rb/library-types';
	import type { SmartlistSummary } from '$lib/rb/api-smartlists';
	import type { AutolistSelection } from '$lib/smartlists/autolist-rule';
	import ColumnBrowser, { type ColumnTrackRow } from './ColumnBrowser.svelte';
	import PlaylistTree from './PlaylistTree.svelte';
	import AutolistBrowser from './AutolistBrowser.svelte';

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
		ondroptracks,
		onautolistchange
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
		onautolistchange: (selection: AutolistSelection, title: string) => void;
	} = $props();

	let mode = $state<'tree' | 'column' | 'autolists'>('tree');
	let columnMounted = $state(false);
	let autolistsMounted = $state(false);

	$effect(() => {
		if (mode === 'column') columnMounted = true;
		if (mode === 'autolists') autolistsMounted = true;
	});

	const treeOptionalProps = $derived.by(() => ({
		...(onselectsmartlist !== undefined ? { onselectsmartlist } : {}),
		...(onselecttrack !== undefined ? { onselecttrack } : {}),
		...(onloadtrack !== undefined ? { onloadtrack } : {}),
		...(oncreateplaylist !== undefined ? { oncreateplaylist } : {}),
		...(onrenameplaylist !== undefined ? { onrenameplaylist } : {}),
		...(ondeleteplaylist !== undefined ? { ondeleteplaylist } : {}),
		...(onduplicateplaylist !== undefined ? { onduplicateplaylist } : {}),
		...(ondroptracks !== undefined ? { ondroptracks } : {})
	}));
</script>

<div class="browse-views-root">
	<div class="view-tabs">
		<button class="vt" class:active={mode === 'tree'} onclick={() => (mode = 'tree')}>
			Tree View
		</button>
		<button class="vt" class:active={mode === 'column'} onclick={() => (mode = 'column')}>
			Column View
		</button>
		<button
			class="vt"
			class:active={mode === 'autolists'}
			data-testid="autolists-tab"
			onclick={() => (mode = 'autolists')}
		>
			Autolists
		</button>
	</div>
	<div class="view-body" class:hidden={mode !== 'tree'}>
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
			{...treeOptionalProps}
		/>
	</div>
	{#if columnMounted}
		<div class="view-body column-mode" class:hidden={mode !== 'column'}>
			<ColumnBrowser selectedId={trackSelectedId} {onselecttrack} {onloadtrack} />
		</div>
	{/if}
	{#if autolistsMounted}
		<div class="view-body autolists-mode" class:hidden={mode !== 'autolists'}>
			<AutolistBrowser onselectionchange={onautolistchange} />
		</div>
	{/if}
</div>

<style>
	.browse-views-root {
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
	.view-body {
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
	}
	.view-body.hidden {
		display: none;
	}
	.view-body.column-mode,
	.view-body.autolists-mode {
		overflow: hidden;
	}
</style>
