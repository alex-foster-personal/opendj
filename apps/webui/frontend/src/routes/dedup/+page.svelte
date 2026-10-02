<script lang="ts">
	/**
	 * /dedup - duplicate review + merge (interactive review UI).
	 *
	 * Renders GET /api/v1/dedup/clusters: one card per cluster with a
	 * side-by-side member comparison (artwork, title/artist, bpm/key,
	 * duration, file_exists, rating) and a survivor picker. Merge POSTs
	 * apply (OpenDJ playlist membership rewrite). keep-all / skip POST a
	 * pending decision only. Undo restores the journaled memberships.
	 */
	import { onMount } from 'svelte';
	import {
		DedupConflictError,
		applyDedupMerge,
		dedupArtworkUrl,
		fetchDedupClusters,
		postDedupDecision,
		undoDedupMerge
	} from './dedup-api';
	import { mergeCueWarning } from './cue-warning';
	import type { Cluster, ClustersResponse, Decision, DecisionAction } from './types';

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

	function patchDecision(clusterKey: string, decision: Decision, revision: string): void {
		if (data === null) return;
		data = {
			...data,
			revision,
			clusters: data.clusters.map((cluster) =>
				cluster.cluster_key === clusterKey ? { ...cluster, decision } : cluster
			)
		};
	}

	async function runWrite(
		cluster: Cluster,
		writer: (survivor: string, revision: string, signal: AbortSignal) => Promise<{
			cluster_key: string;
			survivor: string;
			action: DecisionAction;
			decided_at: string;
			pending_apply: boolean;
			revision: string;
		}>
	): Promise<void> {
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
			const record = await writer(survivor, expectedRevision, signal);
			if (!signal.aborted) {
				patchDecision(clusterKey, {
					cluster_key: record.cluster_key,
					survivor: record.survivor,
					action: record.action,
					decided_at: record.decided_at,
					pending_apply: record.pending_apply
				}, record.revision);
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

	async function decide(cluster: Cluster, action: DecisionAction): Promise<void> {
		await runWrite(cluster, (survivor, revision, signal) =>
			postDedupDecision(cluster.cluster_id, cluster.cluster_key, survivor, action, revision, signal)
		);
	}

	async function applyMerge(cluster: Cluster): Promise<void> {
		const clusterKey = cluster.cluster_key;
		const survivor = selectedSurvivor[clusterKey] ?? cluster.survivor_stable_id;
		const warning = mergeCueWarning(cluster.members, survivor);
		if (warning !== null && !window.confirm(warning)) {
			return;
		}
		await runWrite(cluster, (survivor, revision, signal) =>
			applyDedupMerge(cluster.cluster_id, cluster.cluster_key, survivor, revision, signal)
		);
	}

	async function undoMerge(cluster: Cluster): Promise<void> {
		await runWrite(cluster, (survivor, revision, signal) =>
			undoDedupMerge(cluster.cluster_id, cluster.cluster_key, survivor, revision, signal)
		);
	}

	function decisionBadge(decision: Decision): string {
		if (decision.action === 'merge' && !decision.pending_apply) {
			return `applied: merge -> ${decision.survivor}`;
		}
		if (decision.action === 'merge') {
			return `decided: merge -> ${decision.survivor} (pending apply)`;
		}
		return `decided: ${decision.action} -> ${decision.survivor}`;
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
		Merge rewrites OpenDJ playlist memberships; it does not delete files or copy cue points.
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
				<section class="cluster-card" data-testid="dedup-cluster">
					<header class="cluster-header">
						<h3 title="Duplicate-cluster id assigned by the dedup scan">Cluster #{cluster.cluster_id}</h3>
						{#if cluster.flagged_manual_review}
							<span class="badge flagged">needs manual review</span>
						{/if}
						{#if cluster.rationale}
							<span class="rationale">proposal rationale: {cluster.rationale}</span>
						{/if}
						{#if cluster.decision}
							<span class="badge decided">
								{decisionBadge(cluster.decision)}
							</span>
						{/if}
					</header>

					<div class="members">
						{#each cluster.members as member (member.stable_id)}
						<label
							class="member"
							class:selected={selectedSurvivor[cluster.cluster_key] === member.stable_id}
							data-testid="dedup-member"
						>
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
											<span class="chip" title="Audio-fingerprint similarity to the proposed survivor, 0-100%">similarity {(member.similarity * 100).toFixed(1)}%</span>
										{/if}
										<span class="chip" title="Tempo in beats per minute (- when not analyzed)">{member.bpm ?? '-'} BPM</span>
										<span class="chip" title="Musical key (- when not analyzed)">{member.key ?? '-'}</span>
										<span class="chip" title="Track length, minutes:seconds">{formatDuration(member.duration_ms)}</span>
										<span class="chip" title="Library rating of this copy (- when unrated)">rating {member.rating ?? '-'}</span>
										<span class="chip" class:missing={!member.file_exists}>
											{member.file_exists ? 'file found' : 'file missing'}
										</span>
										<span
											class="chip"
											class:missing={member.cue_count === 0}
											data-testid="dedup-member-cues"
											title="Cue points rekordbox stores for this copy, memory and hot cues together (loops included)"
										>
											{member.cue_count === 0 ? 'no cues' : `${member.cue_count} cues`}
										</span>
										{#if member.hot_cue_count > 0}
											<span class="chip" title="Hot cues (pads A-H) rekordbox stores for this copy">{member.hot_cue_count} hot cues</span>
										{/if}
										{#if member.loop_count > 0}
											<span class="chip" title="Saved loops (cues with a loop end) rekordbox stores for this copy">{member.loop_count} loops</span>
										{/if}
										<span
											class="chip"
											class:missing={!member.has_beatgrid}
											data-testid="dedup-member-beatgrid"
										>
											{member.has_beatgrid ? 'beatgrid' : 'no beatgrid'}
										</span>
										{#if member.cue_positions_ms.length > 0}
											<span
												class="chip"
												data-testid="dedup-member-cue-positions"
												title="Positions of the first 8 cue points, minutes:seconds from the track start"
											>
												{member.cue_positions_ms
													.slice(0, 8)
													.map((position) => formatDuration(position))
													.join(', ')}
											</span>
										{/if}
									</div>
									<div class="member-path">{member.path}</div>
								</div>
							</label>
						{/each}
					</div>

					<div class="decision-row">
						<button
							data-testid="dedup-merge"
							onclick={() => applyMerge(cluster)}
							disabled={posting[cluster.cluster_key]}
						>
							Merge into selected
						</button>
						<button
							data-testid="dedup-keep-all"
							onclick={() => decide(cluster, 'keep-all')}
							disabled={posting[cluster.cluster_key]}
						>
							Keep all
						</button>
						<button
							data-testid="dedup-skip"
							onclick={() => decide(cluster, 'skip')}
							disabled={posting[cluster.cluster_key]}
						>
							Skip
						</button>
						{#if cluster.decision?.action === 'merge' && cluster.decision.pending_apply === false}
							<button
								data-testid="dedup-undo"
								onclick={() => undoMerge(cluster)}
								disabled={posting[cluster.cluster_key]}
							>
								Undo
							</button>
						{/if}
						{#if postError[cluster.cluster_key]}
							<span class="post-error">{postError[cluster.cluster_key]}</span>
						{/if}
					</div>
				</section>
			{/each}
		{/if}
	{:else if !error}
		<p class="empty">Loading dedup clusters from the configured daemon...</p>
	{/if}
</div>

<style>
	.dedup-page h2 {
		margin: 0 0 0.5rem 0;
		font-size: 1.05rem;
	}
	.pending-apply-banner {
		background: color-mix(in srgb, var(--accent) 12%, transparent);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 0.6rem 0.85rem;
		font-size: 0.82rem;
		color: var(--fg);
		margin-bottom: 1rem;
	}
	.error-banner {
		background: var(--danger);
		color: var(--on-danger);
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
		color: var(--accent);
		border: 1px solid var(--accent);
	}
	.badge.decided {
		color: var(--success);
		border: 1px solid var(--success);
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
		border-color: var(--accent);
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
		background: var(--surface-raised);
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
		color: var(--success);
		border-color: var(--success);
	}
	.chip.missing {
		color: var(--danger);
		border-color: var(--danger);
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
