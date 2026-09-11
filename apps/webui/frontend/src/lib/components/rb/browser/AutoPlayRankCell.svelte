<script lang="ts">
	import { autoPlayOrder } from '$lib/rb/autoplay-queue.svelte';

	const AUTOPLAY_ARROW = '\u2193';

	let {
		stableId,
		rank,
		isHot = false,
		onHover
	}: {
		stableId: string;
		rank: number;
		isHot?: boolean;
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
>{rank}{AUTOPLAY_ARROW}</span>
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
	.ap-pin-role {
		display: inline-block;
		margin-left: 2px;
		font-size: 9px;
		color: var(--rb-accent);
		text-transform: lowercase;
	}
</style>
