<script lang="ts">
	// UsbStickTree's rows as props only: the container reads the stick and
	// owns folder toggles; this draws the stick view it is handed.
	import type { PlaylistNode } from '$lib/rb/library-types';
	import type { UsbStickView } from '$lib/rb/usb-library.svelte';

	let {
		volumeName,
		view,
		openFolders,
		errorHeadline,
		selectedId,
		onactivate,
		onrowkeydown
	}: {
		volumeName: string;
		/** undefined until the first read of this stick starts. */
		view: UsbStickView | undefined;
		/** Folder pane ids the user expanded. */
		openFolders: Record<string, boolean>;
		/** Short words for an error view's code; shown only on error. */
		errorHeadline: string;
		selectedId: string | null;
		/** A folder toggles; a list opens in the active pane. */
		onactivate: (node: PlaylistNode) => void;
		onrowkeydown: (event: KeyboardEvent, node: PlaylistNode) => void;
	} = $props();
</script>

{#snippet branch(nodes: PlaylistNode[], depth: number)}
	{#each nodes as node (node.playlist_id)}
		{@const isFolder = node.kind === 'folder'}
		{@const open = openFolders[node.playlist_id] === true}
		<div
			class="row"
			class:selected={!isFolder && selectedId === node.playlist_id}
			style="padding-left: {18 + depth * 12}px"
			role="treeitem"
			tabindex="0"
			aria-selected={!isFolder && selectedId === node.playlist_id}
			aria-expanded={isFolder ? open : undefined}
			data-testid={isFolder ? 'usb-stick-folder' : 'usb-stick-node'}
			data-pane-id={node.playlist_id}
			onclick={() => onactivate(node)}
			onkeydown={(e) => onrowkeydown(e, node)}
		>
			{#if isFolder}
				<svg class="chev" class:open viewBox="0 0 8 8" width="8" height="8" aria-hidden="true">
					<path d="M2 1l4 3-4 3z" fill="currentColor" />
				</svg>
			{:else}
				<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
					<path d="M2 4h12v2H2zM2 8h12v2H2zM2 12h8v2H2z" fill="currentColor" />
				</svg>
			{/if}
			<span class="name" title={node.name}>{node.name}</span>
			<span
				class="count"
				title={isFolder
					? `${node.track_count} lists in this folder on the stick`
					: `${node.track_count} tracks in this list on the stick`}>{node.track_count}</span
			>
		</div>
		{#if isFolder && open}
			{@render branch(node.children, depth + 1)}
		{/if}
	{/each}
{/snippet}

<div class="usb-stick-tree-body">
	{#if view === undefined || view.status === 'loading'}
		<div class="row rb-inert" style="padding-left: 18px" data-testid="usb-stick-loading">
			<span class="name dim">reading stick...</span>
		</div>
	{:else if view.status === 'error'}
		<div
			class="row rb-inert"
			style="padding-left: 18px"
			data-testid="usb-stick-error"
			data-code={view.code}
			title={view.message}
		>
			<span class="name dim">{errorHeadline}</span>
		</div>
	{:else}
		{@render branch(view.tree, 0)}
	{/if}
</div>

<style>
	.row {
		display: flex;
		align-items: center;
		gap: 5px;
		height: 20px;
		padding-right: 6px;
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
	.row svg {
		flex: none;
		color: var(--rb-text-dim);
	}
	.chev {
		transition: transform 80ms linear;
	}
	.chev.open {
		transform: rotate(90deg);
	}
	.name {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.name.dim {
		color: var(--rb-text-dim);
	}
	.count {
		flex: none;
		min-width: 4ch;
		text-align: right;
		color: var(--rb-text-dim);
		font-variant-numeric: tabular-nums;
	}
	.row.rb-inert {
		opacity: 0.6;
		cursor: default;
	}
	.row.rb-inert:hover {
		background: transparent;
	}

	/* A11Y-11811: reduced motion (OS or Appearance); mirrors app.css. */
	@media (prefers-reduced-motion: reduce) {
		:where(*) {
			animation-duration: 0.01ms;
			animation-iteration-count: 1;
			transition-duration: 0.01ms;
		}
	}
	:global(html[data-reduce-motion]) :where(*) {
		animation-duration: 0.01ms;
		animation-iteration-count: 1;
		transition-duration: 0.01ms;
	}
</style>
