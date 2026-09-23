<script lang="ts">
	/**
	 * /progress-tree - canonical feature-progress ledger viewer.
	 *
	 * Renders GET /api/v1/progress (backed by data/progress-tree.yaml):
	 * sticky header with per-status rollup chips that double as status
	 * filter toggles, effort toggles, text filter; collapsible areas of
	 * node rows; footer with meta.updated + file git provenance.
	 * Auto-refetches every REFRESH_INTERVAL_MS while the tab is visible.
	 * Errors are loud (daemon down renders a banner, stale data stays up,
	 * never a blank page).
	 *
	 * Capability-gated: when the daemon is not identified yet, this route
	 * renders one inert panel and issues nothing -- no first load, no refresh
	 * timer, no visibility refetch.
	 */
	import { onMount, tick } from 'svelte';
	import { capabilities, progressRefusal } from '$lib/api/capabilities.svelte';
	import AreaSection from './AreaSection.svelte';
	import DepGraph from './DepGraph.svelte';
	import TierIcon from './TierIcon.svelte';
	import { fetchProgress } from './progress-api';
	import {
		buildReverseDeps,
		EFFORTS,
		hasFoldoutDetail,
		STATUSES,
		TIER_META,
		TIERS,
		type BuildableTier,
		type Effort,
		type NodeStatus,
		type ProgressNode,
		type ProgressResponse
	} from './types';

	/** Auto-refetch cadence while the document is visible. */
	const REFRESH_INTERVAL_MS = 30_000;
	/** How long a dep-jump target stays highlighted. */
	const FLASH_MS = 1600;
	/** localStorage key persisting the Tree/Graph tab choice. */
	const TAB_KEY = 'pt-active-tab';

	type Tab = 'tree' | 'graph';

	let data = $state<ProgressResponse | null>(null);
	let error = $state<string | null>(null);
	let statusFilter = $state<ReadonlySet<NodeStatus>>(new Set(STATUSES));
	let effortFilter = $state<ReadonlySet<Effort>>(new Set(EFFORTS));
	let tierFilter = $state<ReadonlySet<BuildableTier>>(new Set(TIERS));
	let textFilter = $state('');
	let collapsedAreas = $state<ReadonlySet<string>>(new Set());
	let expandedNodes = $state<ReadonlySet<string>>(new Set());
	let activeTab = $state<Tab>('tree');

	/** Flat list of every node across all areas (graph input + expand-all). */
	const allNodes = $derived<ProgressNode[]>(
		(data?.areas ?? []).flatMap((area) => area.nodes)
	);

	/** node id -> ids of nodes that declare it as a dep ("blocks: ..."). */
	const reverseDeps = $derived(buildReverseDeps(allNodes));

	//----- data loading -----------------------------------------------------

	/** Why this route is inert, or null when the daemon serves the ledger. */
	const ledgerRefusal = $derived(progressRefusal());

	async function load(): Promise<void> {
		try {
			data = await fetchProgress();
			error = null;
		} catch (e) {
			// Keep last-good data rendered under the error banner.
			error = e instanceof Error ? e.message : String(e);
		}
	}

	onMount(() => {
		const stored = localStorage.getItem(TAB_KEY);
		if (stored === 'tree' || stored === 'graph') activeTab = stored;
		// Cheap when the layout already probed: the answer is memoized. Probing
		// here too means a deep link to this route resolves on its own.
		void capabilities.probe();
	});

	// Loading lives in an effect keyed on the capability, so an engine-served
	// boot never starts the timer at all, and a probe that resolves late still
	// starts it exactly once. The teardown runs when the capability flips or
	// the route unmounts.
	$effect(() => {
		if (ledgerRefusal !== null) return;
		void load();
		const intervalId = setInterval(() => {
			if (!document.hidden) void load();
		}, REFRESH_INTERVAL_MS);
		const onVisibility = (): void => {
			if (!document.hidden) void load();
		};
		document.addEventListener('visibilitychange', onVisibility);
		return () => {
			clearInterval(intervalId);
			document.removeEventListener('visibilitychange', onVisibility);
		};
	});

	//----- filtering ----------------------------------------------------------

	function nodeMatches(node: ProgressNode): boolean {
		if (!statusFilter.has(node.status)) return false;
		if (!effortFilter.has(node.effort)) return false;
		// A node with no classification yet is never hidden by the tier filter.
		if (node.buildable !== null && !tierFilter.has(node.buildable.tier)) return false;
		const q = textFilter.trim().toLowerCase();
		if (q === '') return true;
		const haystack = [node.id, node.title, node.reuse ?? '', node.notes ?? '']
			.join(' ')
			.toLowerCase();
		return haystack.includes(q);
	}

	const statusCounts = $derived.by((): Record<NodeStatus, number> => {
		const counts = Object.fromEntries(STATUSES.map((s) => [s, 0])) as Record<NodeStatus, number>;
		for (const area of data?.areas ?? []) {
			for (const node of area.nodes) counts[node.status] += 1;
		}
		return counts;
	});

	const tierCounts = $derived.by((): Record<BuildableTier, number> => {
		const counts = Object.fromEntries(TIERS.map((t) => [t, 0])) as Record<BuildableTier, number>;
		for (const area of data?.areas ?? []) {
			for (const node of area.nodes) {
				if (node.buildable !== null) counts[node.buildable.tier] += 1;
			}
		}
		return counts;
	});

	const filtersActive = $derived(
		statusFilter.size !== STATUSES.length ||
			effortFilter.size !== EFFORTS.length ||
			tierFilter.size !== TIERS.length ||
			textFilter.trim() !== ''
	);

	function toggleStatus(status: NodeStatus): void {
		const next = new Set(statusFilter);
		if (next.has(status)) next.delete(status);
		else next.add(status);
		statusFilter = next;
	}

	function toggleEffort(effort: Effort): void {
		const next = new Set(effortFilter);
		if (next.has(effort)) next.delete(effort);
		else next.add(effort);
		effortFilter = next;
	}

	function toggleTier(tier: BuildableTier): void {
		const next = new Set(tierFilter);
		if (next.has(tier)) next.delete(tier);
		else next.add(tier);
		tierFilter = next;
	}

	function resetFilters(): void {
		statusFilter = new Set(STATUSES);
		effortFilter = new Set(EFFORTS);
		tierFilter = new Set(TIERS);
		textFilter = '';
	}

	function toggleArea(areaId: string): void {
		const next = new Set(collapsedAreas);
		if (next.has(areaId)) next.delete(areaId);
		else next.add(areaId);
		collapsedAreas = next;
	}

	//----- tabs + fold-out ----------------------------------------------------

	function setTab(tab: Tab): void {
		activeTab = tab;
		localStorage.setItem(TAB_KEY, tab);
	}

	function toggleExpand(nodeId: string): void {
		const next = new Set(expandedNodes);
		if (next.has(nodeId)) next.delete(nodeId);
		else next.add(nodeId);
		expandedNodes = next;
	}

	function expandAll(): void {
		expandedNodes = new Set(allNodes.filter(hasFoldoutDetail).map((n) => n.id));
	}

	function collapseAll(): void {
		expandedNodes = new Set();
	}

	/** Graph 'view in tree': switch to the Tree tab, expand the node's detail,
	 * then scroll-flash its row. */
	async function viewInTree(nodeId: string): Promise<void> {
		setTab('tree');
		if (hasFoldoutDetail(allNodes.find((n) => n.id === nodeId) ?? ({} as ProgressNode))) {
			expandedNodes = new Set(expandedNodes).add(nodeId);
		}
		await tick();
		await jumpToNode(nodeId);
	}

	//----- dep jump: scroll-to + flash ----------------------------------------

	function areaIdForNode(nodeId: string): string | null {
		for (const area of data?.areas ?? []) {
			if (area.nodes.some((n) => n.id === nodeId)) return area.id;
		}
		return null;
	}

	async function jumpToNode(nodeId: string): Promise<void> {
		const areaId = areaIdForNode(nodeId);
		if (areaId === null) {
			// Validation guarantees deps exist, so this is a real bug if hit.
			error = `dep jump failed: node '${nodeId}' not found in loaded data`;
			return;
		}
		// Make sure the target is actually rendered: expand its area and,
		// if filters hide it, clear them before scrolling.
		if (collapsedAreas.has(areaId)) toggleArea(areaId);
		const target = data?.areas
			.find((a) => a.id === areaId)
			?.nodes.find((n) => n.id === nodeId);
		if (target !== undefined && !nodeMatches(target)) resetFilters();
		await tick();
		const el = document.getElementById(`pt-node-${nodeId}`);
		if (el === null) {
			error = `dep jump failed: element for node '${nodeId}' not rendered`;
			return;
		}
		el.scrollIntoView({ behavior: 'smooth', block: 'center' });
		el.classList.add('flash');
		setTimeout(() => el.classList.remove('flash'), FLASH_MS);
	}
</script>

<svelte:head>
	<title>Progress tree</title>
</svelte:head>

<div class="pt-page">
{#if ledgerRefusal === null && error}
	<div class="error-banner">
		Progress ledger error: {error}
		{#if data}
			<span class="stale-note">(showing last successful fetch)</span>
		{/if}
	</div>
{/if}

{#if ledgerRefusal !== null}
	<!-- INERT: no request was made and no refresh timer is running. -->
	<section class="pt-inert" aria-label="Progress ledger unavailable">
		<h2>Progress tree</h2>
		<p class="inert-note" title={ledgerRefusal}>{ledgerRefusal}</p>
		<p class="inert-detail">
			The fan-out ledger lives on the legacy daemon. Nothing was fetched for this page, so
			there is no ledger data to show and no stale data to mistake for live.
			<code>data/progress-tree.yaml</code>
		</p>
	</section>
{:else if data}
	<div class="pt-header">
		<div class="header-row">
			<h2>Progress tree</h2>
			<span class="branch">branch: {data.meta.branch}</span>
			<div class="tabs" role="tablist">
				<button
					class="tab"
					class:active={activeTab === 'tree'}
					role="tab"
					aria-selected={activeTab === 'tree'}
					onclick={() => setTab('tree')}
				>
					Tree
				</button>
				<button
					class="tab"
					class:active={activeTab === 'graph'}
					role="tab"
					aria-selected={activeTab === 'graph'}
					onclick={() => setTab('graph')}
				>
					Graph
				</button>
			</div>
		</div>
		<div class="filter-row">
			{#each STATUSES as status (status)}
				<button
					class="rollup-chip {status}"
					class:off={!statusFilter.has(status)}
					onclick={() => toggleStatus(status)}
					title="toggle {status} nodes"
				>
					{status} <strong>{statusCounts[status]}</strong>
				</button>
			{/each}
			<span class="sep"></span>
			{#each EFFORTS as effort (effort)}
				<button
					class="effort-toggle"
					class:off={!effortFilter.has(effort)}
					onclick={() => toggleEffort(effort)}
					title="toggle effort {effort}"
				>
					{effort}
				</button>
			{/each}
			<span class="sep"></span>
			{#each TIERS as tier (tier)}
				<button
					class="tier-toggle"
					class:off={!tierFilter.has(tier)}
					style="--tier: {TIER_META[tier].color}"
					onclick={() => toggleTier(tier)}
					title="show only where a node can be built: toggle {TIER_META[tier].label} nodes"
				>
					<TierIcon buildable={{ tier, reason: '' }} size={13} />
					{TIER_META[tier].label} <strong>{tierCounts[tier]}</strong>
				</button>
			{/each}
			<input type="search" placeholder="filter by id / title / reuse / notes" bind:value={textFilter} />
			{#if filtersActive}
				<button class="reset" onclick={resetFilters}>reset</button>
			{/if}
			{#if activeTab === 'tree'}
				<span class="sep"></span>
				<button class="expand-ctl" onclick={expandAll}>expand all</button>
				<button class="expand-ctl" onclick={collapseAll}>collapse all</button>
			{/if}
		</div>
	</div>

	{#if data.areas.length === 0}
		<p class="empty">Ledger is empty: no areas in data/progress-tree.yaml yet.</p>
	{:else if activeTab === 'tree'}
		{#each data.areas as area (area.id)}
			<AreaSection
				{area}
				visibleNodes={area.nodes.filter(nodeMatches)}
				collapsed={collapsedAreas.has(area.id)}
				convention={data.meta.convention}
				{expandedNodes}
				{reverseDeps}
				onToggle={toggleArea}
				onToggleExpand={toggleExpand}
				onJump={(id) => void jumpToNode(id)}
			/>
		{/each}
	{:else}
		<DepGraph
				nodes={allNodes}
				{tierFilter}
				{tierCounts}
				onToggleTier={toggleTier}
				onViewInTree={(id) => void viewInTree(id)}
			/>
	{/if}

	<footer class="pt-footer" title={data.meta.convention}>
		<span>updated {data.meta.updated}</span>
		{#if data.file_git.last_sha !== null}
			<span>
				file {data.file_git.last_sha.slice(0, 7)} by {data.file_git.last_author} on
				{data.file_git.last_date}
			</span>
		{:else}
			<span>file has no committed git history yet</span>
		{/if}
		<code>data/progress-tree.yaml</code>
	</footer>
{:else if !error}
	<p class="empty">Loading progress tree from the configured daemon...</p>
{/if}
</div>

<style>
	/* The app shell's .content has overflow-x: auto, which makes it a
	 * scroll container (overflow-y computes to auto too) and breaks
	 * position: sticky against the body scroll. Neutralize it only
	 * while this page is mounted; shared app.css stays untouched. */
	:global(.content:has(> .pt-page)) {
		overflow-x: visible;
	}
	.error-banner {
		background: var(--danger);
		color: #fff;
		font-weight: 600;
		padding: 0.6rem 1rem;
		border-radius: 6px;
		margin-bottom: 1rem;
	}
	.stale-note {
		font-weight: 400;
		opacity: 0.85;
		margin-left: 0.5rem;
	}
	.pt-inert h2 {
		margin: 0 0 0.4rem 0;
		font-size: 1.05rem;
	}
	.inert-note {
		color: var(--muted);
		font-weight: 600;
		margin: 0 0 0.35rem 0;
	}
	.inert-detail {
		color: var(--muted);
		max-width: 46rem;
		margin: 0;
	}
	.pt-header {
		position: sticky;
		top: 0;
		z-index: 10;
		background: var(--bg);
		padding: 0.25rem 0 0.6rem 0;
		border-bottom: 1px solid var(--border);
		margin-bottom: 0.75rem;
	}
	.header-row {
		display: flex;
		align-items: baseline;
		gap: 0.75rem;
	}
	.header-row h2 {
		margin: 0 0 0.4rem 0;
		font-size: 1.05rem;
	}
	.branch {
		color: var(--muted);
		font-size: 0.78rem;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
	}
	.filter-row {
		display: flex;
		align-items: center;
		gap: 0.35rem;
		flex-wrap: wrap;
	}
	.rollup-chip {
		font-size: 0.72rem;
		padding: 0.1rem 0.5rem;
		border-radius: 999px;
	}
	.rollup-chip.off,
	.effort-toggle.off {
		opacity: 0.35;
	}
	.rollup-chip.missing {
		color: #9aa4b2;
	}
	.rollup-chip.spiked {
		color: #c4b5fd;
	}
	.rollup-chip.building {
		color: #ffb43a;
	}
	.rollup-chip.partial {
		color: #7cc0ff;
	}
	.rollup-chip.built,
	.rollup-chip.verified {
		color: #4ade80;
	}
	.rollup-chip.merged {
		color: #c4a4fb;
	}
	.rollup-chip.user-finalized {
		color: #fef08a;
	}
	.sep {
		width: 1px;
		height: 1.1rem;
		background: var(--border);
		margin: 0 0.3rem;
	}
	.effort-toggle {
		font-size: 0.72rem;
		font-weight: 700;
		padding: 0.1rem 0.5rem;
	}
	.tier-toggle {
		display: inline-flex;
		align-items: center;
		gap: 0.28rem;
		font-size: 0.72rem;
		padding: 0.1rem 0.5rem;
		border-radius: 999px;
		border: 1px solid var(--tier);
		color: var(--tier);
		background: color-mix(in srgb, var(--tier) 12%, transparent);
	}
	.tier-toggle.off {
		opacity: 0.35;
		border-color: var(--border);
	}
	.filter-row input {
		font-size: 0.78rem;
		padding: 0.2rem 0.5rem;
		min-width: 220px;
	}
	.reset {
		font-size: 0.72rem;
		color: var(--accent);
	}
	.tabs {
		margin-left: auto;
		display: flex;
		gap: 0.25rem;
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 0.15rem;
	}
	.tab {
		font-size: 0.78rem;
		font-weight: 600;
		padding: 0.2rem 0.75rem;
		border-radius: 6px;
		color: var(--muted);
		background: transparent;
		border: none;
		cursor: pointer;
	}
	.tab.active {
		color: var(--bg);
		background: var(--accent);
	}
	.expand-ctl {
		font-size: 0.72rem;
		color: var(--accent);
		background: transparent;
		border: 1px solid var(--accent-dim);
		border-radius: 4px;
		padding: 0.1rem 0.45rem;
		cursor: pointer;
	}
	.empty {
		color: var(--muted);
	}
	.pt-footer {
		display: flex;
		gap: 1rem;
		flex-wrap: wrap;
		margin-top: 1.25rem;
		padding-top: 0.6rem;
		border-top: 1px solid var(--border);
		color: var(--muted);
		font-size: 0.75rem;
	}
	.pt-footer code {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
	}
</style>
