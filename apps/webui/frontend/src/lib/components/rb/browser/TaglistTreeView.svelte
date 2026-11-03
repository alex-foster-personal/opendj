<script lang="ts">
	// TaglistTree's rows as props only: the container loads the MyTag catalog
	// and keeps it live; this draws it (null catalog = still loading).
	import type { MyTagCatalog } from '$lib/rb/api-edit-suite';
	import type { PlaylistNode } from '$lib/rb/library-types';
	import { taglistNode, taglistPaneId } from './taglist-nav';

	let {
		catalog,
		error,
		selectedId,
		onselect
	}: {
		catalog: MyTagCatalog | null;
		error: string | null;
		selectedId: string | null;
		onselect: (node: PlaylistNode) => void;
	} = $props();

	function _rowKeydown(event: KeyboardEvent, tag: { name: string; track_count: number }): void {
		if (event.key === 'Enter') onselect(taglistNode(tag));
	}
</script>

<div class="taglist-root" data-menu-target="tree-readonly">
	{#if error !== null}
		<div class="row rb-inert" title={error}>
			<span class="name dim">taglists unavailable</span>
		</div>
	{:else if catalog === null}
		<div class="row rb-inert">
			<span class="name dim">...</span>
		</div>
	{:else if catalog.tags.length === 0}
		<div class="row rb-inert">
			<span class="name dim">No tags yet</span>
		</div>
	{:else}
		{#each catalog.tags as tag (tag.name)}
			<div
				class="row"
				class:selected={selectedId === taglistPaneId(tag.name)}
				data-testid="taglist-row"
				role="button"
				tabindex="0"
				onclick={() => onselect(taglistNode(tag))}
				onkeydown={(e) => _rowKeydown(e, tag)}
			>
				<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
					<path d="M2 4h12v2H2zM2 8h8v2H2zM2 12h10v2H2z" fill="currentColor" />
				</svg>
				<span class="name" title={tag.name}>{tag.name}</span>
				<span class="count" title="{tag.track_count} tracks">{tag.track_count}</span>
			</div>
		{/each}
	{/if}
</div>

<style>
	.taglist-root {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		padding: 2px 0;
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
	.row svg {
		flex: none;
		color: var(--rb-text-dim);
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
		align-self: stretch;
		display: flex;
		align-items: center;
		justify-content: flex-end;
		color: var(--rb-text-dim);
		font-variant-numeric: tabular-nums;
	}
	.row.rb-inert {
		opacity: 0.5;
		cursor: default;
	}
	.row.rb-inert:hover {
		background: transparent;
	}
</style>
