<script lang="ts">
	// HOT CUE pad-mode dropdown below the hot-cue bank (CHROME-11, issue
	// #3886). Lists the modes a hardware controller's performance-pad
	// selector normally exposes. Only Hot Cue is built; every other entry is
	// labeled not-built-yet and renders inert with a tooltip, per the
	// house rule for controls with no real backing.
	//
	// The deck and its main row clip overflow (Deck.svelte), so an absolute
	// menu below the bank was cut off. The menu is position: fixed and
	// placed from the trigger's rect by triggerFloatingAction (placeFloating
	// under the hood): below when it fits, flipped above otherwise, clamped
	// to the viewport.
	import type { DeckId } from '$lib/rb/deck-id';
	import { PAD_MODE_CATALOG, padModeMenuLabel } from '$lib/rb/pad-mode-catalog';
	import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';
	import { plannedTitle } from '$lib/rb/planned-explainers';

	const NOT_BUILT_TIP = plannedTitle('pad-mode-unbuilt');

	let { deckId }: { deckId: DeckId } = $props();

	let open = $state(false);
	let wrapEl: HTMLSpanElement | undefined = $state();
	let triggerEl: HTMLButtonElement | undefined = $state();

	function onWindowPointerDown(e: PointerEvent): void {
		if (!open) return;
		const target = e.target;
		if (target instanceof Node && wrapEl?.contains(target)) return;
		open = false;
	}

	function onWindowKeyDown(e: KeyboardEvent): void {
		if (!open || e.key !== 'Escape') return;
		open = false;
		triggerEl?.focus();
	}
</script>

<svelte:window onpointerdown={onWindowPointerDown} onkeydown={onWindowKeyDown} />

<span class="pad-menu-wrap" bind:this={wrapEl}>
	<button
		bind:this={triggerEl}
		class="rb-lit-button dropdown"
		aria-label={`hot cue menu deck ${deckId}`}
		title="Pad mode menu - lists the pad modes a controller's performance-pad selector offers; modes marked not-built-yet do nothing yet"
		aria-expanded={open}
		data-testid={`hot-cue-menu-deck-${deckId}`}
		onclick={() => (open = !open)}
	>
		HOT CUE
	</button>
	{#if open}
		<div
			class="pad-menu"
			role="menu"
			use:triggerFloatingAction={{ getTrigger: () => triggerEl ?? null, preferred: 'below', gap: 4 }}
		>
			{#each PAD_MODE_CATALOG as entry (entry.id)}
				<button
					type="button"
					class="pad-menu-item"
					class:rb-inert={!entry.built}
					role="menuitem"
					data-pad-mode={entry.id}
					data-built={entry.built}
					disabled={!entry.built}
					title={entry.built ? undefined : NOT_BUILT_TIP}
					onclick={() => (open = false)}
				>
					{padModeMenuLabel(entry)}
				</button>
			{/each}
		</div>
	{/if}
</span>

<style>
	.dropdown {
		align-self: flex-start;
		height: 18px;
		box-sizing: border-box;
	}
	/* CHROME-01: the caret is a CSS border triangle, never a text glyph. */
	.dropdown::after {
		content: '';
		display: inline-block;
		width: 0;
		height: 0;
		/* the gap the space before a glyph used to leave */
		margin-left: 5px;
		vertical-align: middle;
		border-left: 3px solid transparent;
		border-right: 3px solid transparent;
		border-top: 4px solid var(--rb-text-dim);
	}
	.pad-menu-wrap {
		position: relative;
		align-self: flex-start;
	}
	.pad-menu {
		/* fixed escapes the deck's overflow: hidden; left/top are set by
		 * triggerFloatingAction from the trigger rect */
		position: fixed;
		z-index: 1000;
		min-width: 180px;
		background: var(--rb-panel);
		border: 1px solid var(--rb-border);
		border-radius: 4px;
		padding: 4px 0;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.45);
	}
	.pad-menu-item {
		display: block;
		width: 100%;
		text-align: left;
		background: transparent;
		border: none;
		color: var(--rb-text);
		font-size: 11px;
		padding: 5px 10px;
		cursor: pointer;
	}
	.pad-menu-item:hover:not(.rb-inert) {
		background: rgba(255, 255, 255, 0.06);
	}
	.pad-menu-item.rb-inert {
		opacity: 0.55;
		cursor: default;
	}
</style>
