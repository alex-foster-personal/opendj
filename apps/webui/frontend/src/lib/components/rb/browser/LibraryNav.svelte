<script module lang="ts">
	import type { Component } from 'svelte';
	import type { PlaylistNode } from '$lib/rb/library-types';
	import { pushToast } from '$lib/stores.svelte';

	type UsbList = Component<{
		selectedId: string | null;
		onselect: (node: PlaylistNode) => void;
	}>;

	/** The USBs tab body, lazy: stick browsing (list, tree and the Play from
	 * USB store) stays out of the /performance first-paint bundle until the
	 * tab is first opened. Module scope, so the import runs once per session. */
	let UsbListView = $state<UsbList | null>(null);
	let usbListLoad: Promise<void> | null = null;
	/** Why the tab body failed to load; the next visit to the tab retries. */
	let usbListError = $state<string | null>(null);

	function loadUsbList(): void {
		usbListError = null;
		usbListLoad ??= import('./UsbSourceList.svelte').then(
			(mod) => {
				UsbListView = mod.default;
			},
			(exc: unknown) => {
				usbListLoad = null;
				usbListError = exc instanceof Error ? exc.message : String(exc);
				console.error('[usb] USBs tab failed to load:', exc);
			}
		);
	}

	/** The USB sticks panel, lazy for the same reason: it renders nothing
	 * until opened, so its code (first-seen prompt, format report, import
	 * plan launcher) loads on the first open and stays mounted after. */
	let UsbPanelView = $state<Component | null>(null);
	let usbPanelLoad: Promise<void> | null = null;

	/** A stick a reload interrupted mid-copy still owes its analysis. The
	 * usb-import chunk stays lazy unless one is pending (PENDING_IMPORT_KEY);
	 * a chunk that fails to load says so with a Retry (Sol P1, PR #6223). */
	function resumeUsbAnalysisIfPending(): void {
		let pending: string | null;
		try {
			pending = localStorage.getItem('odj.usb.pendingImportJob');
		} catch (err) {
			console.error('[usb] could not read the remembered stick jobs:', err);
			const why = err instanceof Error ? err.message : String(err);
			pushToast(`Sticks opened before the reload could not be read back, so their analysis did not resume: ${why}`, 'error', undefined, err, {}, 'usb-resume-read', {
				label: 'Retry',
				handler: resumeUsbAnalysisIfPending
			});
			return;
		}
		if (pending === null) return;
		import('$lib/rb/usb-import').then(
			(mod) => mod.resumePendingUsbAnalysis(),
			(exc: unknown) => {
				console.error('[usb] could not load the stick module to resume analysis:', exc);
				const why = exc instanceof Error ? exc.message : String(exc);
				pushToast(`Analysis of a stick from before the reload did not resume: ${why}`, 'error', undefined, exc, {}, 'usb-resume-load', {
					label: 'Retry',
					handler: resumeUsbAnalysisIfPending
				});
			}
		);
	}

	/** `onfail` closes the panel, so the next Open or Details click reruns the
	 * import rather than setting an already-true flag (Sol P2, PR #6223). The
	 * failure is said in a toast whose Retry reopens it (Sol P1, PR #6223). */
	function loadUsbPanel(onfail: () => void, retry: () => void): void {
		usbPanelLoad ??= import('../UsbPanel.svelte').then(
			(mod) => {
				UsbPanelView = mod.default;
			},
			(exc: unknown) => {
				usbPanelLoad = null;
				onfail();
				console.error('[usb] USB panel failed to load:', exc);
				const why = exc instanceof Error ? exc.message : String(exc);
				pushToast(`The USB panel did not load: ${why}`, 'error', undefined, exc, {}, 'usb-panel-load', {
					label: 'Retry',
					handler: retry
				});
			}
		);
	}
</script>

<script lang="ts">
	import { onDestroy, onMount, tick } from 'svelte';
	import LibrarySourceTabs, { type LibrarySourceTab } from './LibrarySourceTabs.svelte';
	import PlaylistTree from './PlaylistTree.svelte';
	import type { PlaylistTreeProps } from './playlist-tree-props';
	import TaglistTree from './TaglistTree.svelte';
	import TreeSmartlistSection from './TreeSmartlistSection.svelte';
	import TreeContextMenu from './TreeContextMenu.svelte';
	import AutolistBrowser from './AutolistBrowser.svelte';
	import PlaylistHistoryPanel from './PlaylistHistoryPanel.svelte';
	import { startUsbWatch, stopUsbWatch, usbTracker } from '$lib/rb/usb-tracker.svelte';
	import { uiPrefs, setPlaylistTreeView } from '$lib/rb/prefs.svelte';
	import type { PlaylistTreeViewMode } from '$lib/rb/playlist-tree-view-prefs';
	import { playlistReveal } from '$lib/rb/playlist-reveal.svelte';
	import { libraryWorkspace } from '$lib/library/library-workspace.svelte';
	import { effectiveLibraryLayout, filterTreeByName } from '$lib/rb/skin-layout';

	let {
		onautolistchange,
		...playlistTreeProps
	}: PlaylistTreeProps = $props();

	let activeTab = $state<LibrarySourceTab>('playlists');
	let treeContextMenu = $state<TreeContextMenu | null>(null);
	let treeSmartlistSection = $state<TreeSmartlistSection | null>(null);
	let deleteSmartlistUi: ((sl: { id: string; name: string }) => void) | undefined;
	let autolistsMounted = $state(false);

	$effect(() => {
		if (usbTracker.panelOpen) {
			loadUsbPanel(
				() => (usbTracker.panelOpen = false),
				() => (usbTracker.panelOpen = true)
			);
		}
	});

	const playlistTreeView = $derived(uiPrefs.playlist_tree_view);

	// SKINLAYOUT-8869e: in a three-panel skin's Library workspace this nav is
	// the "Your Library" panel: a heading, the source tabs as pill chips
	// (CSS in CrateDeskView) and a name search over the playlist tree.
	// Gig's browser never shows it.
	const yourLibrary = $derived(
		libraryWorkspace.active &&
			effectiveLibraryLayout(uiPrefs.layout_override.library, uiPrefs.ui_skin) === 'three-panel'
	);
	let treeQuery = $state('');
	// The search filters the tree view only; column view keeps its own drill-down (Codex P2, PR #8869).
	const treeSearch = $derived(yourLibrary && playlistTreeView === 'tree');
	const shownNodes = $derived(treeSearch ? filterTreeByName(playlistTreeProps.nodes, treeQuery) : playlistTreeProps.nodes);

	onMount(() => {
		startUsbWatch();
		resumeUsbAnalysisIfPending();
	});

	onDestroy(() => {
		stopUsbWatch();
	});

	$effect(() => {
		if (activeTab === 'autolists') autolistsMounted = true;
		else if (activeTab === 'usbs') loadUsbList();
	});

	/** CMDK-6848: a playlist row asked into view lives on the Playlists tab. */
	$effect(() => {
		if (playlistReveal.id !== null) activeTab = 'playlists';
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

<div class="library-nav-root" class:your-library={yourLibrary}>
	{#if yourLibrary}
		<h2 class="your-library-title">Your Library</h2>
	{/if}
	<PlaylistHistoryPanel />
	<LibrarySourceTabs
		active={activeTab}
		onchange={(tab) => (activeTab = tab)}
		{playlistTreeView}
		showPlaylistTools={activeTab === 'playlists'}
		onPlaylistTreeViewChange={handlePlaylistTreeViewChange}
	/>
	{#if activeTab === 'autolists'}
		<TreeContextMenu
			bind:this={treeContextMenu}
			onselect={() => {}}
			ondeletesmartlist={(sl) => deleteSmartlistUi?.(sl)}
			oncreatesmartlist={() => void handleNewSmartlist()}
			onrenamesmartlist={(sl) => treeSmartlistSection?.beginRename(sl)}
			onduplicatesmartlist={(sl) => treeSmartlistSection?.duplicateFromMenu(sl)}
		/>
	{/if}
	{#if autolistsMounted}
		<div
			class="autolists-body"
			class:hidden={activeTab !== 'autolists'}
			data-testid="autolists-body"
		>
			<div class="autolist-browser-wrap">
				<AutolistBrowser onselectionchange={(sel, title) => onautolistchange?.(sel, title)} />
			</div>
			{#if activeTab === 'autolists'}
				<div class="autolists-scroll" data-testid="autolists-scroll">
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
			{/if}
		</div>
	{/if}
	{#if treeSearch && activeTab === 'playlists'}
		<input
			class="your-library-search"
			type="search"
			placeholder="Search in Your Library"
			aria-label="Search playlists by name"
			data-testid="your-library-search"
			bind:value={treeQuery}
		/>
	{/if}
	{#if activeTab === 'playlists'}
		<PlaylistTree
			{...playlistTreeProps}
			nodes={shownNodes}
			mode={playlistTreeView}
			oncreatesmartlist={() => void handleNewSmartlist()}
		/>
	{:else if activeTab === 'taglists'}
		<TaglistTree selectedId={playlistTreeProps.selectedId} onselect={playlistTreeProps.onselect} />
	{:else if activeTab !== 'autolists'}
		{#if UsbListView !== null}
			<UsbListView selectedId={playlistTreeProps.selectedId} onselect={playlistTreeProps.onselect} />
		{:else if usbListError !== null}
			<div class="usb-list-state failed" data-testid="usb-list-failed" title={usbListError}>
				USB list failed to load: open the tab again to retry
			</div>
		{:else}
			<div class="usb-list-state" data-testid="usb-list-loading">loading USB sticks...</div>
		{/if}
	{/if}
	{#if UsbPanelView}
		<UsbPanelView />
	{/if}
</div>

<style>
	.library-nav-root {
		position: relative;
		display: flex;
		flex-direction: column;
		min-height: 0;
		height: 100%;
	}
	.your-library-title {
		margin: 0;
		padding: 12px 12px 8px;
		font-size: 16px;
		font-weight: 700;
		color: var(--rb-text);
	}
	.your-library-search {
		margin: 4px 8px 8px;
		padding: 6px 10px;
		border: none;
		border-radius: 4px;
		background: var(--rb-bg);
		color: var(--rb-text);
		font: inherit;
		font-size: 13px;
	}
	.autolists-body.hidden {
		display: none;
	}
	.usb-list-state {
		padding: 2px 6px;
		font-size: 11px;
		color: var(--rb-text-dim);
	}
	.usb-list-state.failed {
		color: var(--rb-red);
	}
	.autolists-body {
		flex: 1 1 0;
		min-height: 0;
		display: flex;
		flex-direction: column;
		overflow: hidden;
	}
	.autolist-browser-wrap {
		flex: 0 1 auto;
		min-height: 0;
		max-height: 50%;
		display: flex;
		flex-direction: column;
		overflow: hidden;
	}
	.autolists-scroll {
		flex: 1 1 0;
		min-height: 44px;
		overflow-y: auto;
		padding: 2px 0;
	}
	@media (max-height: 799px) {
		.autolist-browser-wrap {
			max-height: 40%;
		}
	}
</style>
