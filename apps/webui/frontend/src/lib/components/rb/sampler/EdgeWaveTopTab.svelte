<script lang="ts">
	/**
	 * Top tab shell for one edge (spec 7.4). S7 fills search, filters, and naming.
	 */
	import type { EdgeId } from '$lib/player/sampler/edge-sampler-ui.svelte';

	let {
		edge,
		page,
		el = $bindable()
	}: {
		edge: EdgeId;
		page: number;
		el?: HTMLDivElement | undefined;
	} = $props();
</script>

<div
	bind:this={el}
	class="whead"
	class:rest={false}
	data-edge={edge}
	data-testid="edge-wave-top-{edge}"
>
	<span class="placeholder">Set {page + 1}</span>
	<span class="dim">S7 shell</span>
</div>

<style>
	.whead {
		position: absolute;
		top: 0;
		height: 44px;
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 0 8px;
		overflow: hidden;
		white-space: nowrap;
		background: color-mix(in srgb, var(--rb-panel) 92%, transparent);
		border: 1px solid color-mix(in srgb, var(--rb-border) 70%, transparent);
		box-sizing: border-box;
		pointer-events: auto;
		font: 11px/1.2 var(--rb-font);
		color: var(--rb-text);
	}
	.whead.rest {
		opacity: 0.55;
	}
	.dim {
		color: var(--rb-text-dim);
		font-size: 10px;
	}
</style>
