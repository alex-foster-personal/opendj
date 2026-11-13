<script lang="ts">
	// Issue #9739: the Key column funnel, the 24 keys in the active notation, multi-select
	// (a row in any picked key stays), stored as canonical Camelot so a notation switch keeps
	// the picks (rekordbox Track Filter Key, mimicked).
	import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';
	import { CAMELOT_KEYS, formatKey, shownKeyNotation } from '$lib/rb/key-notation';
	import {
		EMPTY_KEY_FILTER,
		keyFilterActive,
		toggleKeySelection,
		type KeyColumnFilter
	} from './track-table-column-filters';

	let {
		filter,
		onpatch
	}: {
		filter: KeyColumnFilter;
		onpatch: (next: KeyColumnFilter) => void;
	} = $props();

	let open = $state(false);
	let triggerEl = $state<HTMLButtonElement | null>(null);
	let popoverEl = $state<HTMLDivElement | null>(null);

	const active = $derived(keyFilterActive(filter));
	const notation = $derived(shownKeyNotation());
	const label = (key: string): string => formatKey(key, notation) ?? key;
	const triggerTitle = $derived(
		active
			? `Key filter: ${filter.selected.map(label).join(', ')} (click to change)`
			: 'Filter rows by key'
	);

	function _close(): void {
		open = false;
		triggerEl?.focus();
	}

	function _clear(): void {
		onpatch(EMPTY_KEY_FILTER);
		_close();
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

<span class="key-funnel-pop">
	<button
		type="button"
		class="key-funnel-trigger"
		class:lit={active || open}
		bind:this={triggerEl}
		aria-expanded={open}
		aria-haspopup="dialog"
		aria-label="Key filter"
		title={triggerTitle}
		data-testid="key-funnel-trigger"
		onclick={() => (open = !open)}
	>
		<svg viewBox="0 0 16 16" width="9" height="9" aria-hidden="true">
			<path d="M2 3h12l-4.5 5v5l-3-1.5V8z" fill="currentColor" />
		</svg>
	</button>
	{#if open}
		<div
			class="key-funnel-float"
			role="dialog"
			aria-label="Key column filter"
			bind:this={popoverEl}
			use:triggerFloatingAction={{ getTrigger: () => triggerEl, preferred: 'below', gap: 4 }}
		>
			<div class="key-grid">
				{#each CAMELOT_KEYS as key (key)}
					<button
						type="button"
						class="key"
						class:selected={filter.selected.includes(key)}
						aria-pressed={filter.selected.includes(key)}
						data-testid={`key-funnel-${key}`}
						onclick={() => onpatch(toggleKeySelection(filter, key))}
					>
						{label(key)}
					</button>
				{/each}
			</div>
			<div class="actions">
				<button type="button" class="clear" disabled={!active} onclick={_clear}>Clear</button>
			</div>
		</div>
	{/if}
</span>

<style>
	.key-funnel-pop {
		position: relative;
		display: inline-flex;
		align-items: center;
		margin-left: 2px;
	}

	.key-funnel-trigger {
		padding: 0 1px;
		border: none;
		background: transparent;
		color: var(--rb-text-dim, #888);
		cursor: pointer;
		line-height: 0;
	}

	.key-funnel-trigger.lit {
		color: var(--rb-accent, #4fb2ff);
	}

	.key-funnel-float {
		z-index: 40;
		padding: 6px;
		background: var(--rb-panel-bg, #1a1a1a);
		border: 1px solid var(--rb-border, #444);
		border-radius: 4px;
		font-size: 10px;
	}

	.key-grid {
		display: grid;
		grid-template-columns: repeat(12, auto);
		gap: 2px;
	}

	.key {
		font: inherit;
		padding: 2px 4px;
		border: 1px solid var(--rb-border, #555);
		border-radius: 2px;
		background: transparent;
		color: inherit;
		cursor: pointer;
		white-space: nowrap;
	}

	.key.selected {
		background: var(--rb-accent, #4fb2ff);
		color: var(--rb-on-accent, #fff);
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
