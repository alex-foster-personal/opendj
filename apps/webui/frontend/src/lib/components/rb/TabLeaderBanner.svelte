<!--
	AGENT-18: shown only on a FOLLOWER /performance tab. Another tab (or another
	browser) drives the agent-order bus and the engine's UI mirror; this one is
	read-only toward the engine until the operator takes control.
-->
<script lang="ts">
	import { tabLeadershipView, takeTabControl } from '$lib/rb/tab-leadership.svelte';

	const view = $derived(tabLeadershipView.current);
</script>

{#if view.role === 'follower'}
	<div class="tab-leader" role="status" data-testid="tab-leader-banner" data-reason={view.reason}>
		<span>
			{view.reason === 'another-browser'
				? 'Another Open DJ window is in control'
				: 'Another Open DJ tab is in control'}
		</span>
		<button type="button" class="tab-leader-take" data-testid="tab-leader-take" onclick={takeTabControl}>
			Take control
		</button>
	</div>
{/if}

<style>
	.tab-leader {
		position: fixed;
		bottom: 8px;
		left: 50%;
		transform: translateX(-50%);
		z-index: 40;
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 3px 6px 3px 10px;
		border: 1px solid var(--rb-border);
		border-radius: 4px;
		background: var(--rb-panel);
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
		line-height: 16px;
		opacity: 0.92;
	}

	.tab-leader-take {
		padding: 1px 8px;
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		background: transparent;
		color: var(--rb-text);
		font: inherit;
		cursor: pointer;
	}

	.tab-leader-take:hover {
		border-color: var(--rb-text);
	}
</style>
