<script lang="ts">
	import { onDestroy, onMount, tick } from 'svelte';
	import LibrarySourceTabs, { type LibrarySourceTab } from './LibrarySourceTabs.svelte';
	import PlaylistTree from './PlaylistTree.svelte';
	import type { PlaylistTreeProps } from './playlist-tree-props';
	import TaglistTree from './TaglistTree.svelte';
	import TreeSmartlistSection from './TreeSmartlistSection.svelte';
	import TreeContextMenu from './TreeContextMenu.svelte';
	import UsbSourceList from './UsbSourceList.svelte';
	import AutolistBrowser from './AutolistBrowser.svelte';
	import UsbPanel from '../UsbPanel.svelte';
	import PlaylistHistoryPanel from './PlaylistHistoryPanel.svelte';
	import { startUsbWatch, stopUsbWatch } from '$lib/rb/usb-tracker.svelte';
	import { uiPrefs, setPlaylistTreeView } from '$lib/rb/prefs.svelte';
	import type { PlaylistTreeViewMode } from '$lib/rb/playlist-tree-view-prefs';

	let {
		onautolistchange,
		...playlistTreeProps
	}: PlaylistTreeProps = $props();

	let activeTab = $state<LibrarySourceTab>('playlists');
	let treeContextMenu = $state<TreeContextMenu | null>(null);
	let treeSmartlistSection = $state<TreeSmartlistSection | null>(null);
	let deleteSmartlistUi: ((sl: { id: string; name: string }) => void) | undefined;
	let autolistsMounted = $state(false);

	const playlistTreeView = $derived(uiPrefs.playlist_tree_view);

	onMount(() => {
		startUsbWatch();
	});

	onDestroy(() => {
		stopUsbWatch();
	});

	$effect(() => {
		if (activeTab === 'autolists') autolistsMounted = true;
	});

	async function handleNewSmartlist(): Promise<void> {
		activeTab = 'autolists';
		autolistsMounted = true;
		await tick();
		await treeSmartlistSection?.createAndRename();
	}

	function handlePlaylistTreeViewChange(mode: PlaylistTreeViewMode): void {
		setPlaylistTreeView(mode);
	}
</script>

<div class="library-nav-root">
	<PlaylistHistoryPanel />
	<LibrarySourceTabs
		active={activeTab}
		onchange={(tab) => (activeTab = tab)}
		{playlistTreeView}
		showPlaylistTools={activeTab === 'playlists'}
		onPlaylistTreeViewChange={handlePlaylistTreeViewChange}
	/>
	{#if autolistsMounted}
		<div class="autolist-browser-wrap" class:hidden={activeTab !== 'autolists'}>
			<AutolistBrowser onselectionchange={(sel, title) => onautolistchange?.(sel, title)} />
		</div>
	{/if}
	{#if activeTab === 'playlists'}
		<PlaylistTree
			{...playlistTreeProps}
			mode={playlistTreeView}
			oncreatesmartlist={() => void handleNewSmartlist()}
		/>
	{:else if activeTab === 'taglists'}
		<TaglistTree selectedId={playlistTreeProps.selectedId} onselect={playlistTreeProps.onselect} />
	{:else if activeTab === 'autolists'}
		<TreeContextMenu
			bind:this={treeContextMenu}
			onselect={() => {}}
			ondeletesmartlist={(sl) => deleteSmartlistUi?.(sl)}
			oncreatesmartlist={() => void handleNewSmartlist()}
			onrenamesmartlist={(sl) => treeSmartlistSection?.beginRename(sl)}
			onduplicatesmartlist={(sl) => treeSmartlistSection?.duplicateFromMenu(sl)}
		/>
		<div class="autolists-scroll">
			<TreeSmartlistSection
				bind:this={treeSmartlistSection}
				selectedId={playlistTreeProps.selectedId}
				onselectsmartlist={playlistTreeProps.onselectsmartlist}
				{treeContextMenu}
				onDeleteReady={(fn) => {
					deleteSmartlistUi = fn;
				}}
			/>
		</div>
	{:else}
		<UsbSourceList />
	{/if}
	<UsbPanel />
</div>

<style>
	.library-nav-root {
		position: relative;
		display: flex;
		flex-direction: column;
		min-height: 0;
		height: 100%;
	}
	.autolist-browser-wrap.hidden {
		display: none;
	}
	.autolists-scroll {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		padding: 2px 0;
	}
</style>
