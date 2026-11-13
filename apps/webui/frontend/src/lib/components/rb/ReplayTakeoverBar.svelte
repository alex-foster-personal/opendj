<script lang="ts">
	import type { ReplayLiveControl } from '$lib/rb/set-replay-takeover.svelte';
	import { selectorNode } from '$lib/keyboard/selector-register';
	import { bannerLeafSelId, bannerSelId } from './banner-selector-ids';

	interface Props {
		live: readonly ReplayLiveControl[];
		takenOverAll: boolean;
		onTakeOverAll: () => void;
	}

	let { live, takenOverAll, onTakeOverAll }: Props = $props();
</script>

<!-- #14559: a selector group while shown, one leaf per button, so Enter presses that button. -->
<div
	class="replay-takeover-root"
	data-testid="replay-takeover-bar"
	data-replay-taken-over-all={takenOverAll}
	role="status"
	use:selectorNode={{ id: bannerSelId('replay-takeover'), kind: 'group', enabled: true }}
>
	<span class="replay-takeover-label">REPLAY</span>
	{#if takenOverAll}
		<span class="replay-takeover-all-text">Live: all controls</span>
	{:else}
		{#each live.filter((c) => c.control !== 'all') as row (row.control)}
			<span class="replay-takeover-chip" data-replay-live={row.control}>
				{row.control} · {row.source}
			</span>
		{/each}
		<div
			class="sel-leaf"
			use:selectorNode={{
				id: bannerLeafSelId('replay-takeover', 'take-over-all'),
				kind: 'leaf',
				enabled: true
			}}
		>
			<button
				type="button"
				class="replay-takeover-all-btn"
				data-testid="replay-takeover-all"
				onclick={onTakeOverAll}
			>
				Take over all
			</button>
		</div>
	{/if}
</div>

<style>
	.replay-takeover-root {
		position: fixed;
		top: var(--rb-topbar-h);
		left: 0;
		right: 0;
		z-index: 39;
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 10px;
		flex-wrap: wrap;
		padding: 4px 10px;
		border-bottom: 1px solid color-mix(in srgb, var(--rb-accent) 55%, var(--rb-border));
		background: color-mix(in srgb, var(--rb-accent) 14%, var(--rb-panel));
		font-size: 12px;
	}
	.replay-takeover-label {
		font-weight: 600;
		letter-spacing: 0.06em;
	}
	.replay-takeover-chip {
		padding: 2px 8px;
		border-radius: 4px;
		background: color-mix(in srgb, var(--rb-accent) 22%, transparent);
	}
	/* A selector leaf adds no box: the banner's flex row lays out its buttons as before. */
	.sel-leaf {
		display: contents;
	}
	.replay-takeover-all-btn {
		padding: 2px 10px;
		border: 1px solid var(--rb-border);
		border-radius: 4px;
		background: var(--rb-panel);
		cursor: pointer;
	}
	.replay-takeover-all-btn:hover {
		background: color-mix(in srgb, var(--rb-accent) 12%, var(--rb-panel));
	}
</style>
