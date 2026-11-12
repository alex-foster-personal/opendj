<script lang="ts">
	import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';
	import {
		REKORDBOX_TRACK_COLOR_IDS,
		TRACK_COLOUR_NONE,
		clearColourFilter,
		colourFilterActive,
		toggleColourSelection,
		trackColourSwatch,
		type ColourColumnFilter
	} from './track-table-column-filters';

	let {
		filter,
		onpatch
	}: {
		filter: ColourColumnFilter;
		onpatch: (next: ColourColumnFilter) => void;
	} = $props();

	let open = $state(false);
	let triggerEl = $state<HTMLButtonElement | null>(null);
	let popoverEl = $state<HTMLDivElement | null>(null);

	const active = $derived(colourFilterActive(filter));
	const triggerLabel = $derived(active ? `Colour (${filter.selected.length})` : 'Colour');

	function _close(): void {
		open = false;
		triggerEl?.focus();
	}

	function _toggle(): void {
		open = !open;
	}

	function _pick(colorId: number): void {
		onpatch(toggleColourSelection(filter, colorId));
	}

	function _clear(): void {
		onpatch(clearColourFilter());
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

<div class="colour-funnel-pop">
	<button
		type="button"
		class="colour-funnel-trigger"
		class:lit={active || open}
		bind:this={triggerEl}
		aria-expanded={open}
		aria-haspopup="dialog"
		title="Filter rows by rekordbox colour label"
		onclick={_toggle}
	>
		{triggerLabel}
	</button>
	{#if open}
		<div
			class="colour-funnel-float"
			role="dialog"
			aria-label="Colour column filter"
			bind:this={popoverEl}
			use:triggerFloatingAction={{ getTrigger: () => triggerEl, preferred: 'below', gap: 4 }}
		>
			<div class="swatch-grid">
				{#each REKORDBOX_TRACK_COLOR_IDS as colorId (colorId)}
					{@const swatch = trackColourSwatch(colorId)}
					<button
						type="button"
						class="swatch"
						class:selected={filter.selected.includes(colorId)}
						style={swatch === null ? undefined : `background:${swatch}`}
						aria-pressed={filter.selected.includes(colorId)}
						title={`Colour ${colorId}`}
						onclick={() => _pick(colorId)}
					></button>
				{/each}
				<button
					type="button"
					class="swatch none"
					class:selected={filter.selected.includes(TRACK_COLOUR_NONE)}
					aria-pressed={filter.selected.includes(TRACK_COLOUR_NONE)}
					title="No colour"
					onclick={() => _pick(TRACK_COLOUR_NONE)}
				>
					<span class="none-label">0</span>
				</button>
			</div>
			<div class="actions">
				<button type="button" class="clear" disabled={!active} onclick={_clear}>Clear</button>
			</div>
		</div>
	{/if}
</div>

<style>
	.colour-funnel-pop {
		position: relative;
		display: inline-flex;
		align-items: center;
	}

	.colour-funnel-trigger {
		font: inherit;
		font-size: var(--rb-fs-browser, 10px);
		padding: 0 2px;
		border: none;
		background: transparent;
		color: inherit;
		cursor: pointer;
		white-space: nowrap;
	}

	.colour-funnel-trigger.lit {
		color: var(--rb-accent, #4fb2ff);
	}

	.colour-funnel-float {
		z-index: 40;
		padding: 6px;
		background: var(--rb-panel-bg, #1a1a1a);
		border: 1px solid var(--rb-border, #444);
		border-radius: 4px;
		min-width: 120px;
	}

	.swatch-grid {
		display: grid;
		grid-template-columns: repeat(4, 18px);
		gap: 4px;
		justify-content: center;
	}

	.swatch {
		width: 18px;
		height: 18px;
		padding: 0;
		border: 1px solid var(--rb-border, #666);
		border-radius: 2px;
		cursor: pointer;
	}

	.swatch.selected {
		outline: 2px solid var(--rb-accent, #4fb2ff);
		outline-offset: 1px;
	}

	.swatch.none {
		background: repeating-linear-gradient(45deg, #333, #333 2px, #222 2px, #222 4px);
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.none-label {
		font-size: 8px;
		opacity: 0.8;
	}

	.actions {
		margin-top: 6px;
		display: flex;
		justify-content: flex-end;
	}

	.clear {
		font: inherit;
		font-size: 10px;
		padding: 2px 6px;
		cursor: pointer;
	}

	.clear:disabled {
		opacity: 0.4;
		cursor: default;
	}
</style>
