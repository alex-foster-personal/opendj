<script lang="ts">
	import { COLUMN_VIEW_PATH, TREE_LIST_PATH } from '$lib/ui/icon-glyphs';
	import type { PlaylistTreeViewMode } from '$lib/rb/playlist-tree-view-prefs';
	import {
		playlistHistoryChrome,
		redoPlaylistFromChrome,
		undoPlaylistFromChrome
	} from '$lib/rb/playlist-history-chrome.svelte';

	export type LibrarySourceTab = 'playlists' | 'taglists' | 'autolists' | 'usbs';

	let {
		active,
		onchange,
		playlistTreeView = 'tree',
		onPlaylistTreeViewChange,
		showPlaylistTools = false
	}: {
		active: LibrarySourceTab;
		onchange: (tab: LibrarySourceTab) => void;
		playlistTreeView?: PlaylistTreeViewMode;
		onPlaylistTreeViewChange?: (mode: PlaylistTreeViewMode) => void;
		showPlaylistTools?: boolean;
	} = $props();

	const tabs: { id: LibrarySourceTab; label: string; testid: string }[] = [
		{ id: 'playlists', label: 'Playlists', testid: 'library-source-playlists' },
		{ id: 'taglists', label: 'Taglists', testid: 'library-source-taglists' },
		{ id: 'autolists', label: 'Autolists', testid: 'library-source-autolists' },
		{ id: 'usbs', label: 'USBs', testid: 'library-source-usbs' }
	];

	let historyOpen = $state(false);
	let historyAnchor = $state<HTMLButtonElement | null>(null);

	function toggleTreeColumn(): void {
		if (!showPlaylistTools || onPlaylistTreeViewChange === undefined) return;
		onPlaylistTreeViewChange(playlistTreeView === 'tree' ? 'column' : 'tree');
	}

	function openHistory(): void {
		historyOpen = true;
	}

	function closeHistory(): void {
		historyOpen = false;
	}

	const treeToggleTitle: string = $derived(
		showPlaylistTools
			? playlistTreeView === 'tree'
				? 'Switch playlist sidebar to column browser'
				: 'Switch playlist sidebar to tree list'
			: 'Switch to Playlists to change tree or column layout'
	);
</script>

<div class="source-toolbar" data-testid="library-source-tabs">
	<div class="view-tabs" role="tablist" aria-label="library sources">
		{#each tabs as tab (tab.id)}
			<button
				type="button"
				class="vt"
				class:active={active === tab.id}
				role="tab"
				aria-selected={active === tab.id}
				data-testid={tab.testid}
				onclick={() => onchange(tab.id)}
			>
				{tab.label}
			</button>
		{/each}
	</div>
	<div class="trailing-actions" data-testid="library-tree-toolbar">
		<button
			type="button"
			class="icon-btn"
			data-testid="playlist-tree-view-toggle"
			title={treeToggleTitle}
			aria-label={treeToggleTitle}
			disabled={!showPlaylistTools}
			aria-pressed={showPlaylistTools && playlistTreeView === 'column'}
			onclick={toggleTreeColumn}
		>
			<svg viewBox="0 0 24 24" width="12" height="12" aria-hidden="true">
				<path
					d={playlistTreeView === 'tree' ? COLUMN_VIEW_PATH : TREE_LIST_PATH}
					fill="none"
					stroke="currentColor"
					stroke-width="2"
				/>
			</svg>
		</button>
		<button
			type="button"
			class="icon-btn"
			data-testid="playlist-undo"
			title="Undo playlist edit (Ctrl/Cmd+Z). Hover for recent edits."
			disabled={!playlistHistoryChrome.canUndo}
			bind:this={historyAnchor}
			onclick={() => undoPlaylistFromChrome()}
			onmouseenter={openHistory}
			onfocus={openHistory}
		>
			↶
		</button>
		<button
			type="button"
			class="icon-btn"
			data-testid="playlist-redo"
			title="Redo playlist edit (Ctrl/Cmd+Shift+Z)"
			disabled={!playlistHistoryChrome.canRedo}
			onclick={() => redoPlaylistFromChrome()}
		>
			↷
		</button>
	</div>
</div>

{#if historyOpen && historyAnchor !== null}
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		class="history-popover"
		style:z-index="5"
		role="tooltip"
		onmouseleave={closeHistory}
		onblur={closeHistory}
	>
		<p class="history-hint" title="Recent playlist edits at the undo cursor">
			Recent playlist edits
		</p>
		<ol class="history-list" data-testid="playlist-history-list">
			{#each playlistHistoryChrome.entries as entry, index (entry.command_id)}
				<li
					class="history-item"
					class:pending={index >= playlistHistoryChrome.cursor}
					aria-current={index === playlistHistoryChrome.cursor - 1 ? 'step' : undefined}
				>
					{entry.label}
				</li>
			{/each}
		</ol>
	</div>
{/if}

<style>
	.source-toolbar {
		display: flex;
		flex: none;
		align-items: stretch;
		border-bottom: 1px solid var(--rb-border);
		position: relative;
		z-index: 2;
	}
	.view-tabs {
		display: flex;
		flex: 1;
		min-width: 0;
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
	.trailing-actions {
		display: flex;
		flex: none;
		align-items: center;
		gap: 2px;
		padding: 0 4px;
		border-left: 1px solid var(--rb-border);
		background: var(--rb-panel);
	}
	.icon-btn {
		width: 22px;
		height: 22px;
		padding: 0;
		border: 1px solid var(--rb-border);
		background: var(--rb-panel-raised);
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: 12px;
		line-height: 1;
		cursor: pointer;
	}
	.icon-btn svg {
		display: block;
		margin: auto;
	}
	.icon-btn:disabled {
		opacity: 0.45;
		cursor: default;
	}
	.icon-btn:not(:disabled):hover {
		border-color: var(--rb-accent);
		color: var(--rb-accent);
	}
	.history-popover {
		position: absolute;
		top: 100%;
		right: 4px;
		margin-top: 2px;
		min-width: 180px;
		max-width: 260px;
		padding: 4px 6px;
		border: 1px solid var(--rb-border);
		background: var(--rb-panel-raised);
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.35);
	}
	.history-hint {
		margin: 0 0 4px;
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
	}
	.history-list {
		margin: 0;
		padding: 0;
		max-height: 120px;
		overflow-y: auto;
		list-style: none;
	}
	.history-item {
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		color: var(--rb-text);
		padding: 1px 2px;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.history-item.pending {
		color: var(--rb-text-dim);
	}
	.history-item[aria-current='step'] {
		color: var(--rb-accent);
	}
</style>
