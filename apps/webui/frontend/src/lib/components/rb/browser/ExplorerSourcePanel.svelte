<script lang="ts">
	// Issue #9747: the Explorer source (rekordbox Explorer, mimicked). Loads the
	// folder tree over `GET /api/v1/library/folders`; opening a folder hands its
	// path to the browser, which lists its tracks in the active pane through the
	// `browser_select_playlist` performance command (`explorer:<path>`), so
	// agents reach the same rows without this panel.
	import { onMount } from 'svelte';
	import { fetchFolderTree, type FolderTree } from '$lib/explorer/explorer-api';
	import { ancestorPaths } from '$lib/explorer/explorer-pane';
	import ExplorerPanelView from './ExplorerPanelView.svelte';

	let {
		activePath,
		oncollection,
		onopenfolder
	}: {
		/** The folder the active pane shows, or null. */
		activePath: string | null;
		oncollection: () => void;
		/** A folder was opened: list its tracks (or every track below it) in the track table. */
		onopenfolder: (path: string, recursive: boolean) => void;
	} = $props();

	let tree = $state<FolderTree | null>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let expanded = $state<ReadonlySet<string>>(new Set());
	let recursive = $state(false);

	async function loadTree(): Promise<void> {
		loading = true;
		try {
			tree = await fetchFolderTree();
			error = null;
			// Reopening the source keeps the folder the pane shows on screen.
			if (activePath !== null)
				expanded = new Set([...expanded, ...ancestorPaths(tree.roots, activePath)]);
		} catch (exc) {
			error = exc instanceof Error ? exc.message : String(exc);
		} finally {
			loading = false;
		}
	}

	function toggle(path: string): void {
		const next = new Set(expanded);
		if (next.has(path)) next.delete(path);
		else next.add(path);
		expanded = next;
	}

	onMount(() => {
		void loadTree();
	});
</script>

<ExplorerPanelView
	{tree}
	{loading}
	{error}
	{expanded}
	{activePath}
	{recursive}
	{oncollection}
	ontoggle={toggle}
	onopen={(path) => onopenfolder(path, recursive)}
	onrecursive={(value) => (recursive = value)}
/>
