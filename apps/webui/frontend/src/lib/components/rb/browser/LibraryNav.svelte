<script lang="ts">
	import LibrarySourceTabs, { type LibrarySourceTab } from './LibrarySourceTabs.svelte';
	import PlaylistTree from './PlaylistTree.svelte';
	import type { PlaylistTreeProps } from './playlist-tree-props';
	import TaglistTree from './TaglistTree.svelte';
	import TreeSmartlistSection from './TreeSmartlistSection.svelte';
	import TreeContextMenu from './TreeContextMenu.svelte';
	import UsbSourceList from './UsbSourceList.svelte';

	let playlistTreeProps: PlaylistTreeProps = $props();

	let activeTab = $state<LibrarySourceTab>('playlists');
	let treeContextMenu = $state<TreeContextMenu | null>(null);
	let deleteSmartlistUi: ((sl: { id: string; name: string }) => void) | undefined;
</script>

<div class="library-nav-root">
	<LibrarySourceTabs active={activeTab} onchange={(tab) => (activeTab = tab)} />
	{#if activeTab === 'playlists'}
		<PlaylistTree {...playlistTreeProps} />
	{:else if activeTab === 'taglists'}
		<TaglistTree selectedId={playlistTreeProps.selectedId} onselect={playlistTreeProps.onselect} />
	{:else if activeTab === 'autolists'}
		<TreeContextMenu
			bind:this={treeContextMenu}
			onselect={() => {}}
			ondeletesmartlist={(sl) => deleteSmartlistUi?.(sl)}
		/>
		<div class="autolists-scroll">
			<TreeSmartlistSection
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
