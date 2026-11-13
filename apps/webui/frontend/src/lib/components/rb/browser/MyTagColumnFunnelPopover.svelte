<script lang="ts">
	// Issue #9742: the My Tag funnel on the Tags header, the user's My Tags with their track counts,
	// multi-select with AND or OR matching, per pane (rekordbox Track Filter My Tag, mimicked).
	import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';
	import { ensureTagsLoaded, tagState } from '$lib/rb/library-tags.svelte';
	import {
		EMPTY_MYTAG_FILTER,
		mytagFilterActive,
		mytagFilterWithMode,
		toggleMytagSelection,
		type MyTagColumnFilter,
		type MyTagMatchMode
	} from './track-table-column-filters';

	let {
		filter,
		onpatch
	}: {
		filter: MyTagColumnFilter;
		onpatch: (next: MyTagColumnFilter) => void;
	} = $props();

	const uid = $props.id();

	const MODES: readonly { mode: MyTagMatchMode; label: string }[] = [
		{ mode: 'or', label: 'Any (OR)' },
		{ mode: 'and', label: 'All (AND)' }
	];

	let open = $state(false);
	let triggerEl = $state<HTMLButtonElement | null>(null);
	let popoverEl = $state<HTMLDivElement | null>(null);

	const active = $derived(mytagFilterActive(filter));
	const catalog = $derived(open ? tagState.catalog : []);
	const triggerTitle = $derived(
		active
			? `My Tag filter (${filter.mode}): ${filter.selected.join(', ')} (click to change)`
			: 'Filter rows by My Tag'
	);

	function _toggleOpen(): void {
		if (!open) ensureTagsLoaded();
		open = !open;
	}

	function _clear(): void {
		onpatch(EMPTY_MYTAG_FILTER);
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

<span class="mytag-funnel-pop">
	<button
		type="button"
		class="funnel-trigger"
		class:lit={active || open}
		bind:this={triggerEl}
		aria-expanded={open}
		aria-haspopup="dialog"
		aria-label="My Tag filter"
		title={triggerTitle}
		data-testid="mytag-funnel-trigger"
		onclick={_toggleOpen}
	>
		<svg viewBox="0 0 16 16" width="9" height="9" aria-hidden="true">
			<path d="M2 3h12l-4.5 5v5l-3-1.5V8z" fill="currentColor" />
		</svg>
	</button>
	{#if open}
		<div
			class="funnel-float"
			role="dialog"
			aria-label="My Tag column filter"
			bind:this={popoverEl}
			use:triggerFloatingAction={{ getTrigger: () => triggerEl, preferred: 'below', gap: 4 }}
		>
			<div class="mode" role="radiogroup" aria-label="Match">
				{#each MODES as option (option.mode)}
					<label>
						<input
							type="radio"
							name={`mytag-mode-${uid}`}
							checked={filter.mode === option.mode}
							data-testid={`mytag-funnel-mode-${option.mode}`}
							onchange={() => onpatch(mytagFilterWithMode(filter, option.mode))}
						/>
						{option.label}
					</label>
				{/each}
			</div>
			{#if catalog.length === 0}
				<div class="empty">No My Tags yet</div>
			{:else}
				<ul class="mytag-list">
					{#each catalog as tag (tag.name_norm)}
						<li>
							<label>
								<input
									type="checkbox"
									checked={filter.selected.includes(tag.name_norm)}
									data-testid={`mytag-funnel-${tag.name_norm}`}
									onchange={() => onpatch(toggleMytagSelection(filter, tag.name_norm))}
								/>
								<span class="name">{tag.name}</span>
								<span class="count">{tag.track_count}</span>
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
	.mytag-funnel-pop {
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

	.mytag-list {
		list-style: none;
		margin: 0;
		padding: 0;
		max-height: 240px;
		overflow-y: auto;
	}

	.mytag-list label {
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

	.mode {
		display: flex;
		gap: 8px;
		margin-bottom: 4px;
	}

	.mode label {
		display: flex;
		align-items: center;
		gap: 2px;
		cursor: pointer;
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
