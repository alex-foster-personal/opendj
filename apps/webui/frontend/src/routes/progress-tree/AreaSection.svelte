<script lang="ts">
	/**
	 * Collapsible area section. Collapse state lives in the page so
	 * dep-jumps can expand the target's area before scrolling.
	 */
	import NodeRow from './NodeRow.svelte';
	import type { ProgressArea, ProgressNode } from './types';

	let {
		area,
		visibleNodes,
		collapsed,
		convention,
		onToggle,
		onJump
	}: {
		area: ProgressArea;
		visibleNodes: ProgressNode[];
		collapsed: boolean;
		convention: string;
		onToggle: (areaId: string) => void;
		onJump: (id: string) => void;
	} = $props();
</script>

<section class="area">
	<button class="area-header" onclick={() => onToggle(area.id)}>
		<span class="caret">{collapsed ? '+' : '-'}</span>
		<span class="area-title">{area.title}</span>
		<span class="area-count">{visibleNodes.length} of {area.nodes.length} nodes</span>
	</button>
	{#if !collapsed}
		{#if visibleNodes.length === 0}
			<p class="empty-area">No nodes in this area match the current filters.</p>
		{:else}
			{#each visibleNodes as node (node.id)}
				<NodeRow {node} {convention} {onJump} />
			{/each}
		{/if}
	{/if}
</section>

<style>
	.area {
		margin-bottom: 1rem;
	}
	.area-header {
		width: 100%;
		display: flex;
		align-items: center;
		gap: 0.5rem;
		text-align: left;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 0.4rem 0.6rem;
		margin-bottom: 0.25rem;
	}
	.area-header:hover {
		background: #1a212c;
	}
	.caret {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		color: var(--muted);
		width: 1rem;
	}
	.area-title {
		font-weight: 600;
		font-size: 0.9rem;
	}
	.area-count {
		margin-left: auto;
		font-size: 0.75rem;
		color: var(--muted);
	}
	.empty-area {
		color: var(--muted);
		font-size: 0.8rem;
		margin: 0.25rem 0 0.25rem 1.25rem;
	}
</style>
