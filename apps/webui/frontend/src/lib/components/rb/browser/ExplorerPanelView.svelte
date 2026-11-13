<script lang="ts">
	// Issue #9747: the Explorer source in the tree slot, rekordbox Explorer
	// mimicked. Stateless: the folder tree of the collection's stored file
	// paths; the fetching wrapper is ExplorerSourcePanel.svelte.
	import type { FolderTree } from '$lib/explorer/explorer-api';
	import { folderCountLabel, visibleFolderRows } from '$lib/explorer/explorer-pane';

	let {
		tree,
		loading,
		error,
		expanded,
		activePath,
		recursive,
		oncollection,
		ontoggle,
		onopen,
		onrecursive
	}: {
		tree: FolderTree | null;
		loading: boolean;
		error: string | null;
		/** Paths of the folders whose children are on screen. */
		expanded: ReadonlySet<string>;
		/** The folder the active pane shows, or null. */
		activePath: string | null;
		/** Opening a folder lists every track below it, not only its own. */
		recursive: boolean;
		oncollection: () => void;
		ontoggle: (path: string) => void;
		onopen: (path: string) => void;
		onrecursive: (recursive: boolean) => void;
	} = $props();

	const rows = $derived(tree === null ? [] : visibleFolderRows(tree.roots, expanded));
</script>

<div class="explorer-source" data-testid="explorer-panel">
	<div class="source-header">
		<button class="link-btn" onclick={oncollection}>Collection</button>
		<span class="explorer-mark">Explorer</span>
		<label
			class="recursive"
			title="Opening a folder also lists the tracks in every folder below it"
		>
			<input
				type="checkbox"
				data-testid="explorer-recursive"
				checked={recursive}
				onchange={(event) => onrecursive(event.currentTarget.checked)}
			/>
			Subfolders
		</label>
	</div>
	{#if loading && tree === null}
		<p class="status" role="status">Loading folders...</p>
	{:else if error !== null}
		<p class="status error" role="alert">Folders failed to load: {error}</p>
	{:else if rows.length === 0}
		<p class="status" role="status">
			No tracks with a file path yet. Imported tracks show up here by folder.
		</p>
	{:else}
		<ul class="folders" data-testid="explorer-folders" role="tree">
			{#each rows as row (row.node.path)}
				<li
					class="folder"
					class:active={row.node.path === activePath}
					role="treeitem"
					aria-expanded={row.expandable ? row.expanded : undefined}
					aria-selected={row.node.path === activePath}
					style:padding-left="{4 + row.depth * 12}px"
				>
					{#if row.expandable}
						<button
							class="twisty"
							data-testid="explorer-toggle"
							aria-label="{row.expanded ? 'Collapse' : 'Expand'} {row.node.name}"
							onclick={() => ontoggle(row.node.path)}
						>
							<svg
								viewBox="0 0 8 8"
								width="8"
								height="8"
								aria-hidden="true"
								class:open={row.expanded}
							>
								<path
									d="M2.5 1.5L5.5 4L2.5 6.5"
									fill="none"
									stroke="currentColor"
									stroke-width="1.2"
								/>
							</svg>
						</button>
					{:else}
						<span class="twisty" aria-hidden="true"></span>
					{/if}
					<button
						class="folder-name"
						data-testid="explorer-folder"
						data-path={row.node.path}
						title="{row.node.path} - {row.node.direct_track_count} tracks here, {row.node
							.track_count} with subfolders"
						onclick={() => onopen(row.node.path)}
						ondblclick={() => row.expandable && ontoggle(row.node.path)}
					>
						<span class="name">{row.node.name}</span>
						<span class="count">{folderCountLabel(row.node)}</span>
					</button>
				</li>
			{/each}
		</ul>
		{#if tree !== null && tree.unlocated_count > 0}
			<p class="status" data-testid="explorer-unlocated">
				{tree.unlocated_count} tracks have no file path and appear in no folder.
			</p>
		{/if}
	{/if}
</div>

<style>
	.explorer-source {
		display: flex;
		flex-direction: column;
		min-height: 0;
		height: 100%;
		background: var(--rb-panel);
		color: var(--rb-text);
		font-size: var(--rb-fs-label);
	}
	.source-header {
		display: flex;
		align-items: center;
		gap: 6px;
		min-height: 24px;
		padding: 0 6px;
		border-bottom: 1px solid var(--rb-border);
	}
	.explorer-mark {
		font-weight: 600;
	}
	.recursive {
		display: flex;
		align-items: center;
		gap: 2px;
		margin-left: auto;
		color: var(--rb-text-dim);
	}
	.link-btn {
		border: 0;
		padding: 0;
		background: transparent;
		color: var(--rb-accent);
		font: inherit;
		cursor: pointer;
	}
	.status {
		margin: 0;
		padding: 6px;
		color: var(--rb-text-dim);
	}
	.status.error {
		color: var(--rb-danger, #d0342c);
	}
	.folders {
		flex: 1;
		min-height: 0;
		margin: 0;
		padding: 0;
		overflow-y: auto;
		list-style: none;
	}
	.folder {
		display: flex;
		align-items: center;
		height: 22px;
	}
	.folder:hover,
	.folder.active {
		background: var(--rb-panel-raised);
	}
	.twisty {
		flex: 0 0 12px;
		width: 12px;
		padding: 0;
		border: 0;
		background: transparent;
		color: var(--rb-text-dim);
		font: inherit;
		cursor: pointer;
	}
	.twisty svg.open {
		transform: rotate(90deg);
	}
	.folder-name {
		display: flex;
		flex: 1;
		min-width: 0;
		justify-content: space-between;
		gap: 4px;
		height: 100%;
		padding: 0 6px 0 2px;
		border: 0;
		background: transparent;
		color: var(--rb-text);
		font: inherit;
		text-align: left;
		cursor: pointer;
	}
	.name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.count {
		color: var(--rb-text-dim);
		font-variant-numeric: tabular-nums;
	}
</style>
