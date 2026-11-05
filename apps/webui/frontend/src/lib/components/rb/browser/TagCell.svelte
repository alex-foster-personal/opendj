<script lang="ts">
	// R-TAG-1, R-TT-3: a track's tags as italic `#a · #b`, never pills. A type
	// column shows plain names. Clicks stay here so they never select the row.
	import {
		openTagEditor,
		requestTagRemoval,
		tagsCellFor,
		typeColumnFor
	} from '$lib/rb/library-tags.svelte';

	let {
		stableId,
		selectedIds,
		typeId = null,
		label = 'tag'
	}: {
		stableId: string;
		selectedIds: readonly string[];
		typeId?: string | null;
		label?: string;
	} = $props();

	const names = $derived(typeId === null ? tagsCellFor(stableId) : typeColumnFor(stableId, typeId));
	const prefix = $derived(typeId === null ? '#' : '');

	function openEditor(event: MouseEvent): void {
		event.stopPropagation();
		const box = (event.currentTarget as HTMLElement).getBoundingClientRect();
		// Bulk tagging: the whole selection when this row is part of it (R-TAG-6).
		const stableIds = selectedIds.includes(stableId) ? [...selectedIds] : [stableId];
		openTagEditor({ stableIds, typeId, x: box.left, y: box.bottom + 2 });
	}

	function remove(event: MouseEvent, name: string): void {
		event.stopPropagation();
		requestTagRemoval({ stableId, name });
	}
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="tag-cell" data-testid="tag-cell" ondblclick={(e) => e.stopPropagation()}>
	<span class="names" title={names.map((name) => `${prefix}${name}`).join(' · ')}>
		{#each names as name, i (name)}
			{#if i > 0}<span class="sep"> · </span>{/if}<span class="tag"
				>{prefix}{name}<button
					type="button"
					class="remove"
					aria-label={`Remove ${prefix}${name}`}
					onclick={(e) => remove(e, name)}
				>
					<svg viewBox="0 0 8 8" width="7" height="7" aria-hidden="true">
						<path d="M1 1l6 6M7 1L1 7" stroke="currentColor" stroke-width="1.2" fill="none" />
					</svg>
				</button></span
			>
		{/each}
	</span>
	<button type="button" class="add" data-testid="tag-add" onclick={openEditor}>
		{typeId === null ? `+${label}` : '+add'}
	</button>
</div>

<style>
	.tag-cell {
		position: relative;
		display: flex;
		align-items: center;
		min-width: 0;
		height: 100%;
	}
	.names {
		flex: 0 1 auto;
		min-width: 0;
		overflow: hidden;
		white-space: nowrap;
		text-overflow: ellipsis;
		font-style: italic;
	}
	.sep {
		color: var(--rb-text-dim);
		font-style: normal;
	}
	.remove {
		display: none;
		padding: 0 0 0 2px;
		border: 0;
		background: none;
		color: var(--rb-text-dim);
		cursor: pointer;
	}
	.tag:hover .remove,
	.remove:focus-visible {
		display: inline;
	}
	.remove:hover {
		color: var(--rb-text);
	}
	.add {
		flex: none;
		margin-left: 6px;
		padding: 0;
		border: 0;
		background: none;
		color: var(--rb-text-dim);
		font: inherit;
		opacity: 0;
		cursor: pointer;
	}
	:global(tr:hover) .add,
	.add:focus-visible {
		opacity: 1;
	}
	/* D12: dim, and green once the pointer is over the cell itself. */
	.tag-cell:hover .add {
		color: var(--rb-green, #4caf6e);
	}
</style>
