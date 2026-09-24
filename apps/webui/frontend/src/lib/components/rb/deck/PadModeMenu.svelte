<script lang="ts">
	// HOT CUE pad-mode dropdown below the hot-cue bank (CHROME-11, issue
	// #3886). Lists the modes a hardware controller's performance-pad
	// selector normally exposes. Only Hot Cue is built; every other entry is
	// labeled not-built-yet and renders inert with a tooltip, per the
	// house rule for controls with no real backing.
	import type { DeckId } from '$lib/rb/deck-id';
	import { PAD_MODE_CATALOG, padModeMenuLabel } from '$lib/rb/pad-mode-catalog';

	const NOT_BUILT_TIP = 'not implemented - see PARITY-TODO';

	let { deckId }: { deckId: DeckId } = $props();

	let open = $state(false);
</script>

<span class="pad-menu-wrap">
	<button
		class="rb-lit-button dropdown"
		aria-label={`hot cue menu deck ${deckId}`}
		title="Pad mode menu - lists the pad modes a controller's performance-pad selector offers; modes marked not-built-yet do nothing yet"
		aria-expanded={open}
		data-testid={`hot-cue-menu-deck-${deckId}`}
		onclick={() => (open = !open)}
	>
		HOT CUE <span class="caret">&#9662;</span>
	</button>
	{#if open}
		<div class="pad-menu" role="menu">
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
	.caret {
		color: var(--rb-text-dim);
	}
	.pad-menu-wrap {
		position: relative;
		align-self: flex-start;
	}
	.pad-menu {
		position: absolute;
		left: 0;
		top: calc(100% + 4px);
		z-index: 20;
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
