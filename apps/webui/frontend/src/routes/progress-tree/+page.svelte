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
	 */
	import { onMount, tick } from 'svelte';
	import AreaSection from './AreaSection.svelte';
	import { fetchProgress } from './progress-api';
	import {
		EFFORTS,
		STATUSES,
		type Effort,
		type NodeStatus,
		type ProgressNode,
		type ProgressResponse
	} from './types';

	/** Auto-refetch cadence while the document is visible. */
	const REFRESH_INTERVAL_MS = 30_000;
	/** How long a dep-jump target stays highlighted. */
	const FLASH_MS = 1600;

	let data = $state<ProgressResponse | null>(null);
	let error = $state<string | null>(null);
	let statusFilter = $state<ReadonlySet<NodeStatus>>(new Set(STATUSES));
	let effortFilter = $state<ReadonlySet<Effort>>(new Set(EFFORTS));
	let textFilter = $state('');
	let collapsedAreas = $state<ReadonlySet<string>>(new Set());

	//----- data loading -----------------------------------------------------

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

	const filtersActive = $derived(
		statusFilter.size !== STATUSES.length ||
			effortFilter.size !== EFFORTS.length ||
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

	function resetFilters(): void {
		statusFilter = new Set(STATUSES);
		effortFilter = new Set(EFFORTS);
		textFilter = '';
	}

	function toggleArea(areaId: string): void {
		const next = new Set(collapsedAreas);
		if (next.has(areaId)) next.delete(areaId);
		else next.add(areaId);
		collapsedAreas = next;
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
{#if error}
	<div class="error-banner">
		Progress ledger error: {error}
		{#if data}
			<span class="stale-note">(showing last successful fetch)</span>
		{/if}
	</div>
{/if}

{#if data}
	<div class="pt-header">
		<div class="header-row">
			<h2>Progress tree</h2>
			<span class="branch">branch: {data.meta.branch}</span>
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
			<input type="search" placeholder="filter by id / title / reuse / notes" bind:value={textFilter} />
			{#if filtersActive}
				<button class="reset" onclick={resetFilters}>reset</button>
			{/if}
		</div>
	</div>

	{#if data.areas.length === 0}
		<p class="empty">Ledger is empty: no areas in data/progress-tree.yaml yet.</p>
	{:else}
		{#each data.areas as area (area.id)}
			<AreaSection
				{area}
				visibleNodes={area.nodes.filter(nodeMatches)}
				collapsed={collapsedAreas.has(area.id)}
				convention={data.meta.convention}
				onToggle={toggleArea}
				onJump={(id) => void jumpToNode(id)}
			/>
		{/each}
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
	<p class="empty">Loading progress tree from daemon at :8585...</p>
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
	.rollup-chip.working,
	.rollup-chip.verified {
		color: #4ade80;
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
	.filter-row input {
		font-size: 0.78rem;
		padding: 0.2rem 0.5rem;
		min-width: 220px;
	}
	.reset {
		font-size: 0.72rem;
		color: var(--accent);
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
