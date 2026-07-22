<script lang="ts">
	/**
	 * One ledger node as a tree row. The surface stays compact: chevron (only
	 * when there is fold-out detail), status chip, title, effort, one coloured
	 * lane badge per LANE tag, a dep count and a commit (short-SHA) count, plus
	 * the red 'no provenance' tag. Rows with more than a little detail expand
	 * (click row or Enter/Space) into the shared NodeDetail fold-out. Expansion
	 * state is owned by the page so 'expand all' and graph 'view in tree' can
	 * drive it.
	 */
	import NodeDetail from './NodeDetail.svelte';
	import StatusChip from './StatusChip.svelte';
	import TierIcon from './TierIcon.svelte';
	import {
		BUILD_STATE_GLYPH,
		BUILD_STATE_LABEL,
		hasFoldoutDetail,
		hasProvenanceGap,
		laneColor,
		noteSegments,
		parseLanes,
		staleBuildLabel,
		type ProgressNode
	} from './types';

	let {
		node,
		convention,
		expanded,
		blocks = [],
		onToggleExpand,
		onJump
	}: {
		node: ProgressNode;
		convention: string;
		expanded: boolean;
		blocks?: string[];
		onToggleExpand: (id: string) => void;
		onJump: (id: string) => void;
	} = $props();

	const staleLabel = $derived(staleBuildLabel(node.build?.updated ?? null));

	const lanes = $derived(parseLanes(node.notes));
	const expandable = $derived(hasFoldoutDetail(node));
	/** Short notes on a non-expandable row are shown inline so nothing hides. */
	const inlineNote = $derived(expandable ? '' : noteSegments(node.notes).join(' | '));

	function toggle(): void {
		if (expandable) onToggleExpand(node.id);
	}

	function onKey(e: KeyboardEvent): void {
		if (!expandable) return;
		if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			onToggleExpand(node.id);
		}
	}
</script>

<div class="node-row" id={`pt-node-${node.id}`} class:expanded>
	<!-- tabindex is only ever set alongside role="button" (expandable rows) -->
	<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
	<div
		class="main-line"
		class:expandable
		role={expandable ? 'button' : undefined}
		tabindex={expandable ? 0 : undefined}
		aria-expanded={expandable ? expanded : undefined}
		onclick={toggle}
		onkeydown={onKey}
	>
		<span class="chevron" class:hidden={!expandable} aria-hidden="true">
			{expanded ? '▾' : '▸'}
		</span>
		<StatusChip status={node.status} />
		{#if node.buildable}
			<TierIcon buildable={node.buildable} />
		{/if}
		{#if node.build?.state}
			<span
				class="build-glyph state-{node.build.state}"
				title="build: {BUILD_STATE_LABEL[node.build.state]}{node.build.stage
					? ` - ${node.build.stage}`
					: ''}"
			>
				{BUILD_STATE_GLYPH[node.build.state]}
			</span>
		{/if}
		{#if staleLabel}
			<span class="stale-tag" title="build.updated: {node.build?.updated}">{staleLabel}</span>
		{/if}
		<span class="title" title={node.id}>{node.title}</span>
		<span class="effort effort-{node.effort}">{node.effort}</span>
		{#each lanes as lane (lane)}
			<span class="lane" style="--lane: {laneColor(lane)}" title="LANE {lane}">{lane}</span>
		{/each}
		{#if node.reuse}
			<span class="reuse" title="reuse">reuse: {node.reuse}</span>
		{/if}
		{#if hasProvenanceGap(node)}
			<span class="no-provenance" title={convention}>no provenance</span>
		{/if}
		<span class="spacer"></span>
		{#if blocks.length > 0}
			<span class="count blocks" title="blocks {blocks.length} node{blocks.length === 1 ? '' : 's'}">
				blocks {blocks.length}
			</span>
		{/if}
		{#if node.deps.length > 0}
			<span class="count deps" title="{node.deps.length} dependencies">
				{node.deps.length} dep{node.deps.length === 1 ? '' : 's'}
			</span>
		{/if}
		{#if node.commits.length > 0}
			<span class="count sha" title="{node.commits.length} commits">
				{node.commits.length} sha
			</span>
		{/if}
	</div>
	{#if inlineNote !== ''}
		<div class="inline-note">{inlineNote}</div>
	{/if}
	{#if expandable && expanded}
		<NodeDetail {node} {blocks} {onJump} />
	{/if}
</div>

<style>
	.node-row {
		border-bottom: 1px solid var(--border);
		border-radius: 4px;
	}
	.node-row.expanded {
		background: #10151d;
	}
	/* :global so page-level jumpToNode can add .flash via classList */
	:global(.node-row.flash) {
		animation: pt-flash 1.6s ease-out;
	}
	@keyframes pt-flash {
		0% {
			background: #3a3010;
		}
		100% {
			background: transparent;
		}
	}
	.main-line {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		flex-wrap: wrap;
		padding: 0.35rem 0.5rem 0.35rem 0.4rem;
		border-radius: 4px;
	}
	.main-line.expandable {
		cursor: pointer;
	}
	.main-line.expandable:hover {
		background: #141a24;
	}
	.main-line:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: -2px;
	}
	.chevron {
		width: 1rem;
		text-align: center;
		color: var(--muted);
		font-size: 0.7rem;
	}
	.chevron.hidden {
		visibility: hidden;
	}
	.title {
		font-size: 0.88rem;
	}
	.effort {
		font-size: 0.68rem;
		font-weight: 700;
		padding: 0.05rem 0.35rem;
		border-radius: 4px;
		border: 1px solid var(--border);
		color: var(--muted);
	}
	.effort-L {
		color: var(--accent);
		border-color: var(--accent-dim);
	}
	.lane {
		font-size: 0.66rem;
		font-weight: 700;
		padding: 0.05rem 0.4rem;
		border-radius: 999px;
		color: var(--lane);
		border: 1px solid var(--lane);
		background: color-mix(in srgb, var(--lane) 14%, transparent);
	}
	.reuse {
		font-size: 0.75rem;
		color: var(--muted);
		font-style: italic;
	}
	.no-provenance {
		font-size: 0.7rem;
		font-weight: 700;
		color: #fff;
		background: var(--danger);
		padding: 0.05rem 0.4rem;
		border-radius: 4px;
		cursor: help;
	}
	.spacer {
		flex: 1 1 auto;
	}
	.count {
		font-size: 0.7rem;
		padding: 0.05rem 0.4rem;
		border-radius: 4px;
		border: 1px solid var(--border);
		color: var(--muted);
		white-space: nowrap;
	}
	.count.sha {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
	}
	.count.blocks {
		color: #ffb43a;
		border-color: #7a5a1f;
	}
	.build-glyph {
		font-size: 0.85rem;
		line-height: 1;
		cursor: help;
	}
	.build-glyph.state-active {
		color: #4ade80;
		animation: nr-pulse 1.6s ease-in-out infinite;
	}
	.build-glyph.state-idle {
		color: #7cc0ff;
	}
	.build-glyph.state-blocked {
		color: #ffb43a;
	}
	.build-glyph.state-hanging {
		color: var(--danger);
	}
	@keyframes nr-pulse {
		0%,
		100% {
			opacity: 1;
		}
		50% {
			opacity: 0.4;
		}
	}
	.stale-tag {
		font-size: 0.66rem;
		color: var(--muted);
		opacity: 0.75;
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 0.02rem 0.35rem;
		cursor: help;
	}
	.inline-note {
		margin: 0 0 0.3rem 1.9rem;
		font-size: 0.75rem;
		color: var(--muted);
	}
</style>
