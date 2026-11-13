<script lang="ts">
	// Issue #9740: the Genre column funnel, the genres present in the current list with their
	// row counts, multi-select (a row in any picked genre stays), matched ignoring letter case
	// like the tracks API (rekordbox Track Filter Genre, mimicked).
	import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';
	import type { BrowserRow } from './pane-contract.svelte';
	import {
		EMPTY_GENRE_FILTER,
		genreFacets,
		genreFilterActive,
		toggleGenreSelection,
		type GenreColumnFilter
	} from './track-table-column-filters';

	let {
		filter,
		rows,
		onpatch
	}: {
		filter: GenreColumnFilter;
		/** The pane rows before the column funnels, so a pick never hides its own genre. */
		rows: readonly Pick<BrowserRow, 'genre'>[];
		onpatch: (next: GenreColumnFilter) => void;
	} = $props();

	let open = $state(false);
	let triggerEl = $state<HTMLButtonElement | null>(null);
	let popoverEl = $state<HTMLDivElement | null>(null);

	const active = $derived(genreFilterActive(filter));
	const facets = $derived(open ? genreFacets(rows) : []);
	const triggerTitle = $derived(
		active
			? `Genre filter: ${filter.selected.join(', ')} (click to change)`
			: 'Filter rows by genre'
	);

	function _clear(): void {
		onpatch(EMPTY_GENRE_FILTER);
		_close();
	}

	function _close(): void {
		open = false;
		triggerEl?.focus();
	}

	function onWindowKeydown(event: KeyboardEvent): void {
		if (!open || event.key !== 'Escape') return;
		event.preventDefault();
		_close();
	}

	function onWindowPointerDown(event: PointerEvent): void {
		if (!open) return;
		const target = event.target;
		if (!(target instanceof Node)) return;
		if (triggerEl?.contains(target)) return;
		if (popoverEl?.contains(target)) return;
		_close();
	}
</script>

<svelte:window onkeydown={onWindowKeydown} onpointerdown={onWindowPointerDown} />

<span class="genre-funnel-pop">
	<button
		type="button"
		class="funnel-trigger"
		class:lit={active || open}
		bind:this={triggerEl}
		aria-expanded={open}
		aria-haspopup="dialog"
		aria-label="Genre filter"
		title={triggerTitle}
		data-testid="genre-funnel-trigger"
		onclick={() => (open = !open)}
	>
		<svg viewBox="0 0 16 16" width="9" height="9" aria-hidden="true">
			<path d="M2 3h12l-4.5 5v5l-3-1.5V8z" fill="currentColor" />
		</svg>
	</button>
	{#if open}
		<div
			class="funnel-float"
			role="dialog"
			aria-label="Genre column filter"
			bind:this={popoverEl}
			use:triggerFloatingAction={{ getTrigger: () => triggerEl, preferred: 'below', gap: 4 }}
		>
			{#if facets.length === 0}
				<div class="empty">No genres in this list</div>
			{:else}
				<ul class="genre-list">
					{#each facets as facet (facet.value)}
						<li>
							<label>
								<input
									type="checkbox"
									checked={filter.selected.includes(facet.value)}
									data-testid={`genre-funnel-${facet.value}`}
									onchange={() => onpatch(toggleGenreSelection(filter, facet.value))}
								/>
								<span class="name">{facet.label}</span>
								<span class="count">{facet.count}</span>
							</label>
						</li>
					{/each}
				</ul>
			{/if}
			<div class="actions">
				<button type="button" class="clear" disabled={!active} onclick={_clear}>Clear</button>
			</div>
		</div>
	{/if}
</span>

<style>
	.genre-funnel-pop {
		position: relative;
		display: inline-flex;
		align-items: center;
		margin-left: 2px;
	}

	.funnel-trigger {
		padding: 0 1px;
		border: none;
		background: transparent;
		color: var(--rb-text-dim, #888);
		cursor: pointer;
		line-height: 0;
	}

	.funnel-trigger.lit {
		color: var(--rb-accent, #4fb2ff);
	}

	.funnel-float {
		z-index: 40;
		padding: 6px;
		min-width: 140px;
		background: var(--rb-panel-bg, #1a1a1a);
		border: 1px solid var(--rb-border, #444);
		border-radius: 4px;
		font-size: 10px;
	}

	.genre-list {
		list-style: none;
		margin: 0;
		padding: 0;
		max-height: 240px;
		overflow-y: auto;
	}

	.genre-list label {
		display: flex;
		align-items: center;
		gap: 4px;
		cursor: pointer;
		white-space: nowrap;
	}

	.name {
		flex: 1;
	}

	.count,
	.empty {
		color: var(--rb-text-dim, #888);
	}

	.actions {
		margin-top: 6px;
		display: flex;
		justify-content: flex-end;
	}

	.clear {
		font: inherit;
		padding: 2px 6px;
		cursor: pointer;
	}

	.clear:disabled {
		opacity: 0.4;
		cursor: default;
	}
</style>
