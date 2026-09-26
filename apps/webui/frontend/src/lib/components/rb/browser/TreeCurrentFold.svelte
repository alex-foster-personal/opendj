<script lang="ts">
	// Sticky CURRENT fold control (pin 2ac3a0), extracted out of
	// PlaylistTree.svelte. Purely presentational - the scroll/position
	// tracking that decides `fold` lives in tree-fold-tracker.svelte.ts,
	// wired up by PlaylistTree since it owns the scrollable tree and the
	// row elements the tracker watches.
	let {
		fold,
		onjump
	}: {
		fold: 'above' | 'below' | null;
		onjump: () => void;
	} = $props();
</script>

{#if fold !== null}
	<button
		type="button"
		class="current-fold"
		class:above={fold === 'above'}
		class:below={fold === 'below'}
		onclick={onjump}
		title={fold === 'above'
			? 'Current playlist is above - click to jump'
			: 'Current playlist is below - click to jump'}
	>
		{fold === 'above' ? '▲' : '▼'} CURRENT
	</button>
{/if}

<style>
	/* Pin 2ac3a0: CURRENT fold - mirrors TrackTable's .master-fold styling and
	 * positioning so the two "jump to the thing that scrolled off" controls
	 * read as one family. */
	.current-fold {
		position: absolute;
		left: 50%;
		transform: translateX(-50%);
		z-index: 4;
		padding: 3px 14px;
		border: 1px solid var(--rb-accent);
		border-radius: 3px;
		background: color-mix(in srgb, var(--rb-accent) 88%, #08131a);
		color: #08131a;
		font-family: var(--rb-font);
		font-size: 10px;
		font-weight: 700;
		letter-spacing: 0.06em;
		cursor: pointer;
		box-shadow: 0 2px 10px rgba(0, 0, 0, 0.45);
		pointer-events: auto;
	}
	.current-fold:hover {
		background: color-mix(in srgb, var(--rb-accent) 100%, #08131a 0%);
	}
	.current-fold.above {
		top: 2px;
	}
	.current-fold.below {
		bottom: 4px;
	}
</style>
