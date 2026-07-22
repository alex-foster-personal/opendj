<script lang="ts">
	/**
	 * /dedup - duplicate review + merge (interactive review UI).
	 *
	 * Renders GET /api/v1/dedup/clusters: one card per cluster with a
	 * side-by-side member comparison (artwork, title/artist, bpm/key,
	 * duration, file_exists, rating) and a survivor picker. Decision
	 * buttons POST to /api/v1/dedup/clusters/{id}/decision, which stores
	 * ONLY a decision record - no playlist rewrite happens here. The
	 * banner below is the load-bearing disclosure of that split.
	 */
	import { onMount } from 'svelte';
	import {
		DedupConflictError,
		dedupArtworkUrl,
		fetchDedupClusters,
		postDedupDecision
	} from './dedup-api';
	import type { Cluster, ClustersResponse, DecisionAction } from './types';

	let data = $state<ClustersResponse | null>(null);
	let error = $state<string | null>(null);
	let selectedSurvivor = $state<Record<string, string>>({});
	let posting = $state<Record<string, boolean>>({});
	let postError = $state<Record<string, string>>({});
	let pageController: AbortController | null = null;
	let loadSequence = 0;

	async function load(signal: AbortSignal): Promise<void> {
		const sequence = ++loadSequence;
		try {
			const nextData = await fetchDedupClusters(signal);
			if (signal.aborted || sequence !== loadSequence) return;
			data = nextData;
			error = null;
			const next: Record<string, string> = {};
			for (const cluster of data.clusters) {
				const memberIds = new Set(cluster.members.map((member) => member.stable_id));
				const previous = selectedSurvivor[cluster.cluster_key];
				next[cluster.cluster_key] =
					(previous !== undefined && memberIds.has(previous) ? previous : undefined) ??
					(cluster.decision !== null && memberIds.has(cluster.decision.survivor)
						? cluster.decision.survivor
						: cluster.survivor_stable_id);
			}
			selectedSurvivor = next;
		} catch (caught) {
			if (signal.aborted || sequence !== loadSequence) return;
			error = caught instanceof Error ? caught.message : String(caught);
		}
	}

	onMount(() => {
		const controller = new AbortController();
		pageController = controller;
		void load(controller.signal);
		return () => {
			controller.abort();
			if (pageController === controller) pageController = null;
		};
	});

	function selectSurvivor(clusterKey: string, stableId: string): void {
		selectedSurvivor = { ...selectedSurvivor, [clusterKey]: stableId };
	}

	async function decide(cluster: Cluster, action: DecisionAction): Promise<void> {
		const signal = pageController?.signal;
		if (data === null || signal === undefined || signal.aborted) return;
		const clusterKey = cluster.cluster_key;
		const survivor = selectedSurvivor[clusterKey] ?? cluster.survivor_stable_id;
		if (!cluster.members.some((member) => member.stable_id === survivor)) {
			postError = { ...postError, [clusterKey]: 'selected survivor is no longer in this cluster' };
			return;
		}
		const expectedRevision = data.revision;
		posting = { ...posting, [clusterKey]: true };
		postError = { ...postError, [clusterKey]: '' };
		try {
			const record = await postDedupDecision(
				cluster.cluster_id,
				clusterKey,
				survivor,
				action,
				expectedRevision,
				signal
			);
			if (!signal.aborted && data !== null) {
				data = {
					...data,
					revision: record.revision,
					clusters: data.clusters.map((c) =>
						c.cluster_key === clusterKey
							? {
									...c,
									decision: {
										cluster_key: record.cluster_key,
										survivor: record.survivor,
										action: record.action,
										decided_at: record.decided_at
									}
								}
							: c
					)
				};
			}
		} catch (caught) {
			if (signal.aborted) return;
			if (caught instanceof DedupConflictError) {
				await load(signal);
			}
			postError = {
				...postError,
				[clusterKey]: caught instanceof Error ? caught.message : String(caught)
			};
		} finally {
			if (!signal.aborted) posting = { ...posting, [clusterKey]: false };
		}
	}

	function formatDuration(ms: number | null): string {
		if (ms === null) return '-';
		const totalSeconds = Math.round(ms / 1000);
		const m = Math.floor(totalSeconds / 60);
		const s = totalSeconds % 60;
		return `${m}:${String(s).padStart(2, '0')}`;
	}

	function memberLabel(member: Cluster['members'][number]): string {
		const parts = [member.title ?? member.stable_id];
		if (member.artist) parts.push(`- ${member.artist}`);
		return parts.join(' ');
	}
</script>

<svelte:head>
	<title>Duplicate review</title>
</svelte:head>

<div class="dedup-page">
	<h2>Duplicate review + merge</h2>

	<div class="pending-apply-banner">
		Decisions stored, apply runs locally - see PARITY-TODO. This page never rewrites playlist
		references; it only records what you decided so a local, real-library run can apply it later.
	</div>

	{#if error}
		<div class="error-banner">
			Dedup review error: {error}
			{#if data}<span class="stale-note">(showing last successful fetch)</span>{/if}
		</div>
	{/if}

	{#if data}
		{#if data.note}
			<p class="empty">{data.note}</p>
		{:else if data.clusters.length === 0}
			<p class="empty">No duplicate clusters found.</p>
		{:else}
			{#each data.clusters as cluster (cluster.cluster_key)}
				<section class="cluster-card">
					<header class="cluster-header">
						<h3>Cluster #{cluster.cluster_id}</h3>
						{#if cluster.flagged_manual_review}
							<span class="badge flagged">needs manual review</span>
						{/if}
						{#if cluster.rationale}
							<span class="rationale">proposal rationale: {cluster.rationale}</span>
						{/if}
						{#if cluster.decision}
							<span class="badge decided">
								decided: {cluster.decision.action} -&gt; {cluster.decision.survivor} (pending apply)
							</span>
						{/if}
					</header>

					<div class="members">
						{#each cluster.members as member (member.stable_id)}
						<label class="member" class:selected={selectedSurvivor[cluster.cluster_key] === member.stable_id}>
							<input
								type="radio"
								name={`survivor-${cluster.cluster_key}`}
								value={member.stable_id}
								checked={selectedSurvivor[cluster.cluster_key] === member.stable_id}
								onchange={() => selectSurvivor(cluster.cluster_key, member.stable_id)}
								/>
								<img
									class="artwork"
									src={dedupArtworkUrl(member.stable_id)}
									alt=""
									loading="lazy"
									onerror={(e) => ((e.currentTarget as HTMLImageElement).style.visibility = 'hidden')}
								/>
								<div class="member-info">
									<div class="member-title">{memberLabel(member)}</div>
									<div class="member-meta">
										{#if member.is_canonical}<span class="chip canonical">proposed survivor</span>{/if}
										{#if member.similarity !== null}
											<span class="chip">similarity {(member.similarity * 100).toFixed(1)}%</span>
										{/if}
										<span class="chip">{member.bpm ?? '-'} BPM</span>
										<span class="chip">{member.key ?? '-'}</span>
										<span class="chip">{formatDuration(member.duration_ms)}</span>
										<span class="chip">rating {member.rating ?? '-'}</span>
										<span class="chip" class:missing={!member.file_exists}>
											{member.file_exists ? 'file found' : 'file missing'}
										</span>
									</div>
									<div class="member-path">{member.path}</div>
								</div>
							</label>
						{/each}
					</div>

					<div class="decision-row">
						<button onclick={() => decide(cluster, 'merge')} disabled={posting[cluster.cluster_key]}>
							Merge into selected
						</button>
						<button onclick={() => decide(cluster, 'keep-all')} disabled={posting[cluster.cluster_key]}>
							Keep all
						</button>
						<button onclick={() => decide(cluster, 'skip')} disabled={posting[cluster.cluster_key]}>
							Skip
						</button>
						{#if postError[cluster.cluster_key]}
							<span class="post-error">{postError[cluster.cluster_key]}</span>
						{/if}
					</div>
				</section>
			{/each}
		{/if}
	{:else if !error}
		<p class="empty">Loading dedup clusters from daemon at :8585...</p>
	{/if}
</div>

<style>
	.dedup-page h2 {
		margin: 0 0 0.5rem 0;
		font-size: 1.05rem;
	}
	.pending-apply-banner {
		background: var(--muted-bg, rgba(255, 180, 58, 0.12));
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 0.6rem 0.85rem;
		font-size: 0.82rem;
		color: var(--fg);
		margin-bottom: 1rem;
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
	.empty {
		color: var(--muted);
	}
	.cluster-card {
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 0.85rem 1rem;
		margin-bottom: 1rem;
	}
	.cluster-header {
		display: flex;
		align-items: center;
		gap: 0.6rem;
		flex-wrap: wrap;
		margin-bottom: 0.6rem;
	}
	.cluster-header h3 {
		margin: 0;
		font-size: 0.92rem;
	}
	.rationale {
		color: var(--muted);
		font-size: 0.78rem;
	}
	.badge {
		font-size: 0.72rem;
		padding: 0.1rem 0.5rem;
		border-radius: 999px;
	}
	.badge.flagged {
		color: #ffb43a;
		border: 1px solid #ffb43a;
	}
	.badge.decided {
		color: #4ade80;
		border: 1px solid #4ade80;
	}
	.members {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
		gap: 0.6rem;
	}
	.member {
		display: flex;
		gap: 0.5rem;
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 0.5rem;
		cursor: pointer;
	}
	.member.selected {
		border-color: var(--accent, #4ade80);
	}
	.member input[type='radio'] {
		align-self: flex-start;
		margin-top: 0.2rem;
	}
	.artwork {
		width: 48px;
		height: 48px;
		object-fit: cover;
		border-radius: 4px;
		background: var(--muted-bg, rgba(255, 255, 255, 0.06));
		flex-shrink: 0;
	}
	.member-info {
		min-width: 0;
	}
	.member-title {
		font-size: 0.85rem;
		font-weight: 600;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.member-meta {
		display: flex;
		flex-wrap: wrap;
		gap: 0.3rem;
		margin: 0.25rem 0;
	}
	.chip {
		font-size: 0.68rem;
		padding: 0.05rem 0.4rem;
		border-radius: 999px;
		border: 1px solid var(--border);
		color: var(--muted);
	}
	.chip.canonical {
		color: #4ade80;
		border-color: #4ade80;
	}
	.chip.missing {
		color: #ff6b6b;
		border-color: #ff6b6b;
	}
	.member-path {
		font-size: 0.68rem;
		color: var(--muted);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
	}
	.decision-row {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		margin-top: 0.75rem;
	}
	.post-error {
		color: var(--danger);
		font-size: 0.78rem;
	}
</style>
