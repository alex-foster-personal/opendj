<script lang="ts">
	/**
	 * Graph tab: the dependency DAG as a dependency-free inline SVG (no d3, no
	 * libs). Nodes are laid out in wave columns (wave 0 left -> wave N right),
	 * coloured by status via the shared STATUS_PALETTE, with a lane halo per
	 * LANE tag. Edges are cubic beziers from dep -> dependent, highlighted when
	 * either endpoint is hovered or selected. A lane legend dims non-lane nodes
	 * on hover. Clicking a chip opens a side panel with the same fold-out detail
	 * as the tree rows plus a 'view in tree' link.
	 *
	 * Pan is native: the SVG sits in an overflow:auto scroll container. Wave
	 * column counts are computed live and never hardcoded.
	 */
	import NodeDetail from './NodeDetail.svelte';
	import { buildGraphLayout, edgePath, type GraphNodeBox } from './graph-layout';
	import { laneColor, STATUS_PALETTE, type ProgressNode } from './types';

	let {
		nodes,
		onViewInTree
	}: {
		nodes: ProgressNode[];
		onViewInTree: (id: string) => void;
	} = $props();

	let showAll = $state(false);
	let hoveredId = $state<string | null>(null);
	let selectedId = $state<string | null>(null);
	let hoveredLane = $state<string | null>(null);

	const layout = $derived(buildGraphLayout(nodes, showAll));
	const selected = $derived(
		selectedId === null ? null : (layout.boxById.get(selectedId)?.node ?? null)
	);

	/** Node ids on either end of a hovered/selected node, for edge + chip focus. */
	const activeId = $derived(hoveredId ?? selectedId);
	const neighbours = $derived.by((): Set<string> => {
		const set = new Set<string>();
		if (activeId === null) return set;
		set.add(activeId);
		for (const edge of layout.edges) {
			if (edge.from === activeId) set.add(edge.to);
			if (edge.to === activeId) set.add(edge.from);
		}
		return set;
	});

	function edgeHot(from: string, to: string): boolean {
		return from === activeId || to === activeId;
	}

	function chipDimmed(box: GraphNodeBox): boolean {
		if (hoveredLane !== null && !box.lanes.includes(hoveredLane)) return true;
		if (activeId !== null && !neighbours.has(box.node.id)) return true;
		return box.done && activeId === null && hoveredLane === null;
	}

	function truncate(title: string, max: number): string {
		return title.length > max ? `${title.slice(0, max - 1)}…` : title;
	}

	function select(id: string): void {
		selectedId = selectedId === id ? null : id;
	}
</script>

<div class="graph-tab">
	<div class="graph-controls">
		<label class="show-all">
			<input type="checkbox" bind:checked={showAll} />
			show all ({nodes.length})
		</label>
		{#if layout.allLanes.length > 0}
			<div class="legend" role="group" aria-label="lane legend">
				{#each layout.allLanes as lane (lane)}
					<button
						class="legend-item"
						class:active={hoveredLane === lane}
						style="--lane: {laneColor(lane)}"
						onmouseenter={() => (hoveredLane = lane)}
						onmouseleave={() => (hoveredLane = null)}
						onfocus={() => (hoveredLane = lane)}
						onblur={() => (hoveredLane = null)}
					>
						<span class="swatch"></span>{lane}
					</button>
				{/each}
			</div>
		{/if}
	</div>

	{#if layout.edges.length === 0}
		<p class="graph-note">
			No dependency edges among the visible nodes - every node stands alone (single column).
		</p>
	{/if}

	<div class="graph-scroll">
		<svg
			class="graph-svg"
			viewBox="0 0 {layout.width} {layout.height}"
			width={layout.width}
			height={layout.height}
			role="img"
			aria-label="dependency graph, {layout.boxes.length} nodes"
		>
			{#each layout.columns as col (col.key)}
				<text class="col-header" x={col.x} y="20">{col.label}</text>
			{/each}

			<g class="edges">
				{#each layout.edges as edge (edge.id)}
					{@const from = layout.boxById.get(edge.from)}
					{@const to = layout.boxById.get(edge.to)}
					{#if from && to}
						<path
							class="edge"
							class:hot={edgeHot(edge.from, edge.to)}
							d={edgePath(from, to)}
						/>
					{/if}
				{/each}
			</g>

			<g class="nodes">
				{#each layout.boxes as box (box.node.id)}
					{@const pal = STATUS_PALETTE[box.node.status]}
					{@const laneStroke = box.lanes.length > 0 ? laneColor(box.lanes[0]) : pal.border}
					<g
						class="chip"
						class:dimmed={chipDimmed(box)}
						class:selected={selectedId === box.node.id}
						role="button"
						tabindex="0"
						aria-label="{box.node.title} ({box.node.status})"
						onmouseenter={() => (hoveredId = box.node.id)}
						onmouseleave={() => (hoveredId = null)}
						onfocus={() => (hoveredId = box.node.id)}
						onblur={() => (hoveredId = null)}
						onclick={() => select(box.node.id)}
						onkeydown={(e) => {
							if (e.key === 'Enter' || e.key === ' ') {
								e.preventDefault();
								select(box.node.id);
							}
						}}
					>
						<title>{box.node.title} [{box.node.status}]</title>
						<rect
							x={box.x}
							y={box.y}
							width={box.w}
							height={box.h}
							rx="8"
							fill={pal.bg}
							stroke={pal.border}
						/>
						<rect
							class="lane-stripe"
							x={box.x}
							y={box.y}
							width="5"
							height={box.h}
							rx="2"
							fill={laneStroke}
						/>
						<text class="chip-title" x={box.x + 14} y={box.cy - 2} fill={pal.fg}>
							{truncate(box.node.title, 22)}
						</text>
						<text class="chip-status" x={box.x + 14} y={box.cy + 13} fill={pal.fg}>
							{box.node.status}{box.done ? ' (met)' : ''}
						</text>
					</g>
				{/each}
			</g>
		</svg>
	</div>

	{#if selected}
		<aside class="side-panel">
			<div class="side-head">
				<span class="side-title">{selected.title}</span>
				<button class="close" onclick={() => (selectedId = null)} title="close">&#10005;</button>
			</div>
			<div class="side-meta">
				<code>{selected.id}</code>
				<span>status: {selected.status}</span>
				<span>effort: {selected.effort}</span>
			</div>
			<NodeDetail node={selected} onJump={(id) => select(id)} />
			<button class="view-tree" onclick={() => onViewInTree(selected.id)}>
				&#8599; view in tree
			</button>
		</aside>
	{/if}
</div>

<style>
	.graph-tab {
		position: relative;
	}
	.graph-controls {
		display: flex;
		align-items: center;
		gap: 1rem;
		flex-wrap: wrap;
		margin-bottom: 0.6rem;
	}
	.show-all {
		display: inline-flex;
		align-items: center;
		gap: 0.35rem;
		font-size: 0.78rem;
		color: var(--fg);
	}
	.legend {
		display: flex;
		flex-wrap: wrap;
		gap: 0.3rem;
	}
	.legend-item {
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
		font-size: 0.68rem;
		padding: 0.1rem 0.4rem;
		border-radius: 999px;
		border: 1px solid var(--border);
		color: var(--muted);
		background: transparent;
	}
	.legend-item.active {
		color: var(--fg);
		border-color: var(--lane);
	}
	.swatch {
		width: 0.7rem;
		height: 0.7rem;
		border-radius: 3px;
		background: var(--lane);
	}
	.graph-note {
		color: var(--muted);
		font-size: 0.8rem;
		margin: 0 0 0.5rem 0;
	}
	.graph-scroll {
		overflow: auto;
		max-height: 72vh;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--bg);
	}
	.graph-svg {
		display: block;
	}
	.col-header {
		font-size: 12px;
		font-weight: 700;
		fill: var(--muted);
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
	}
	.edge {
		fill: none;
		stroke: #3a4250;
		stroke-width: 1.4;
		opacity: 0.6;
	}
	.edge.hot {
		stroke: var(--accent);
		stroke-width: 2.4;
		opacity: 1;
	}
	.chip {
		cursor: pointer;
		transition: opacity 0.12s ease;
	}
	.chip.dimmed {
		opacity: 0.32;
	}
	.chip.selected rect {
		stroke: var(--accent);
		stroke-width: 2.4;
	}
	.chip:focus-visible {
		outline: none;
	}
	.chip:focus-visible rect {
		stroke: var(--accent);
		stroke-width: 2.4;
	}
	.chip-title {
		font-size: 12px;
		font-weight: 600;
	}
	.chip-status {
		font-size: 10px;
		opacity: 0.85;
	}
	.side-panel {
		position: absolute;
		top: 3rem;
		right: 0;
		width: min(24rem, 90%);
		max-height: 70vh;
		overflow: auto;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 10px;
		box-shadow: 0 8px 30px rgba(0, 0, 0, 0.5);
		padding: 0.6rem 0.7rem 0.8rem;
		z-index: 5;
	}
	.side-head {
		display: flex;
		align-items: baseline;
		gap: 0.5rem;
	}
	.side-title {
		font-weight: 600;
		font-size: 0.92rem;
		flex: 1 1 auto;
	}
	.close {
		font-size: 0.85rem;
		color: var(--muted);
		background: transparent;
		border: none;
		cursor: pointer;
	}
	.side-meta {
		display: flex;
		flex-wrap: wrap;
		gap: 0.6rem;
		font-size: 0.72rem;
		color: var(--muted);
		margin: 0.3rem 0 0.2rem;
	}
	.side-meta code {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
	}
	.view-tree {
		margin-top: 0.5rem;
		font-size: 0.76rem;
		color: var(--accent);
		background: transparent;
		border: 1px solid var(--accent-dim);
		border-radius: 4px;
		padding: 0.2rem 0.5rem;
		cursor: pointer;
	}
</style>
