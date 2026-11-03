<script lang="ts">
	import { autoPlayOrder } from '$lib/rb/autoplay-queue.svelte';

	let {
		stableId,
		rank,
		isHot = false,
		arrowDirection = 0,
		arrowColor = null,
		onHover
	}: {
		stableId: string;
		rank: number;
		isHot?: boolean;
		/** +1 next track is below this row, -1 above, 0 when this is the last hop. */
		arrowDirection?: -1 | 0 | 1;
		arrowColor?: string | null;
		onHover: (stableId: string | null) => void;
	} = $props();

	const pinRole = $derived(autoPlayOrder.pinRoleOf.get(stableId) ?? null);
</script>

<span
	class="ap-rank"
	class:ap-rank-hot={isHot}
	tabindex="0"
	title={`AutoPlay queue position ${rank}: hand off after ${rank - 1} more, from the current AutoPlay view`}
	onpointerenter={() => onHover(stableId)}
	onpointerleave={() => onHover(null)}
	onfocus={() => onHover(stableId)}
	onblur={() => onHover(null)}
>{rank}{#if arrowDirection !== 0}<svg
		class="ap-rank-arrow"
		viewBox="0 0 8 10"
		aria-hidden="true"
		style={arrowColor ? `color:${arrowColor}` : ''}
	><path d={arrowDirection < 0 ? 'M3 10H5V5H8L4 0L0 5H3Z' : 'M3 0H5V5H8L4 10L0 5H3Z'} /></svg>{/if}</span>
{#if pinRole}
	<span
		class="ap-pin-role"
		data-pin-role={pinRole}
		title={`Pinned as ${pinRole}`}
	>{pinRole}</span>
{/if}

<style>
	.ap-rank {
		display: inline-block;
		font-style: italic;
		font-size: 10px;
		color: var(--rb-text-dim);
		cursor: default;
		outline: none;
	}
	.ap-rank-hot,
	.ap-rank:focus {
		color: var(--rb-accent);
	}
	.ap-rank-arrow {
		display: inline-block;
		width: 6px;
		height: 8px;
		margin-left: 1px;
		vertical-align: -1px;
		fill: currentColor;
		overflow: visible;
	}
	.ap-pin-role {
		display: inline-block;
		margin-left: 2px;
		font-size: 9px;
		color: var(--rb-accent);
		text-transform: lowercase;
	}
</style>
