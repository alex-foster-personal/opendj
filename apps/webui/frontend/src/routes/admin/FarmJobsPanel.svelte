<script lang="ts">
	/**
	 * Backstage farm jobs (issue #11242, parent #2413): read-only status for long-
	 * running engine work. Rows come from GET /api/v1/jobs (via jobsStore); the
	 * CloudSync scheduler last result from GET /api/v1/cloudsync/status. No
	 * re-enqueue or cancel here yet.
	 */
	import { onMount } from 'svelte';
	import { jobsRefusal } from '$lib/api/capabilities.svelte';
	import { getStatus, schedulerLastResult, type CloudSyncStatus } from '$lib/api-cloudsync';
	import {
		FARM_ENGINE_JOB_KINDS,
		FARM_JOB_KIND_LABELS,
		farmActiveJobs,
		formatDurationMs,
		formatJobTime,
		jobRunDurationMs,
		jobsStore,
		lastFinishedFarmJob,
		type FarmEngineJobKind,
		type Job
	} from '$lib/rb/jobs-store.svelte';

	const POLL_MS = 10_000;

	let cloudStatus = $state<CloudSyncStatus | null>(null);
	let cloudError = $state<string | null>(null);
	let cloudLoaded = $state(false);

	const refusal = $derived(jobsRefusal());
	const activeRows = $derived(farmActiveJobs(jobsStore.jobs));

	const activeByKind = $derived.by(() => {
		const grouped: Record<FarmEngineJobKind, Job[]> = {
			'stems.separate': [],
			'cloud.hydrate': [],
			'anlz.cache-rebuild': []
		};
		for (const row of activeRows) {
			if (row.kind in grouped) grouped[row.kind as FarmEngineJobKind].push(row);
		}
		for (const kind of FARM_ENGINE_JOB_KINDS) {
			grouped[kind].sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
		}
		return grouped;
	});

	const lastSyncResult = $derived(cloudStatus === null ? null : schedulerLastResult(cloudStatus));

	function rowTitle(job: Job): string {
		const parts = [
			`kind ${job.kind}`,
			`status ${job.status}`,
			`created ${job.created_at}`,
			job.started_at ? `started ${job.started_at}` : 'not started',
			job.finished_at ? `finished ${job.finished_at}` : 'not finished'
		];
		return parts.join(' | ');
	}

	function lastRunTitle(job: Job | null): string {
		if (job === null) return 'No finished run in the current jobs list (limit 200, newest first)';
		const duration = jobRunDurationMs(job);
		const durationLabel = duration === null ? 'duration unknown' : formatDurationMs(duration);
		return `Last run ${job.status} | ${durationLabel} (finished minus started)`;
	}

	async function refreshCloud(): Promise<void> {
		try {
			cloudStatus = await getStatus();
			cloudError = null;
			cloudLoaded = true;
		} catch (exc) {
			cloudError = exc instanceof Error ? exc.message : String(exc);
		}
	}

	onMount(() => {
		const detachJobs = refusal === null ? jobsStore.attach() : () => undefined;
		void refreshCloud();
		const timer = setInterval(() => {
			if (refusal === null) void jobsStore.hydrate();
			void refreshCloud();
		}, POLL_MS);
		return () => {
			clearInterval(timer);
			detachJobs();
		};
	});
</script>

<div class="farm-jobs">
	<h3>Farm jobs</h3>
	<p class="copy">
		Engine background work: demucs stems (<code>stems.separate</code>), cloud hydrate (<code
			>cloud.hydrate</code
		>), and ANLZ cache rebuild when that kind registers. Rows are from <code>GET /api/v1/jobs</code>
		(limit 200); CloudSync scheduler last result from
		<code>GET /api/v1/cloudsync/status</code>.
	</p>

	{#if refusal !== null}
		<div class="fatal" title={refusal}>{refusal}</div>
	{:else if jobsStore.error !== null}
		<div class="fatal" title="The previous list is still shown">
			Jobs list error: {jobsStore.error}
		</div>
	{/if}

	{#each FARM_ENGINE_JOB_KINDS as kind (kind)}
		{@const label = FARM_JOB_KIND_LABELS[kind]}
		{@const active = activeByKind[kind]}
		{@const last = lastFinishedFarmJob(jobsStore.jobs, kind)}
		<section class="kind-block">
			<h4 title={`engine kind ${kind}`}>{label}</h4>
			<p class="meta" title={lastRunTitle(last)}>
				Last run:
				{#if last === null}
					<em>none in list</em>
				{:else}
					<code>{last.status}</code>
					{#if jobRunDurationMs(last) !== null}
						, <span title="finished_at minus started_at"
							>{formatDurationMs(jobRunDurationMs(last)!)}</span
						>
					{/if}
					<span class="times">
						(started {formatJobTime(last.started_at)}, finished {formatJobTime(last.finished_at)})
					</span>
				{/if}
			</p>

			{#if active.length === 0}
				<p class="empty">No queued, running, or failed rows for this kind.</p>
			{:else}
				<table>
					<thead>
						<tr>
							<th scope="col">status</th>
							<th scope="col">progress</th>
							<th scope="col">created</th>
							<th scope="col">started</th>
							<th scope="col">finished</th>
							<th scope="col">id</th>
						</tr>
					</thead>
					<tbody>
						{#each active as row (row.id)}
							<tr title={rowTitle(row)}>
								<td><code>{row.status}</code></td>
								<td title="{Math.round(row.progress * 100)}%">{Math.round(row.progress * 100)}%</td>
								<td>{formatJobTime(row.created_at)}</td>
								<td>{formatJobTime(row.started_at)}</td>
								<td>{formatJobTime(row.finished_at)}</td>
								<td><code class="id">{row.id}</code></td>
							</tr>
						{/each}
					</tbody>
				</table>
			{/if}
		</section>
	{/each}

	<section class="cloudsync">
		<h4 title="CloudSync scheduler, not an engine job row">CloudSync scheduler</h4>
		{#if cloudError !== null}
			<div class="fatal" title={cloudError}>CloudSync status: {cloudError}</div>
		{:else if !cloudLoaded}
			<p class="sub">Loading CloudSync status...</p>
		{:else if lastSyncResult === null}
			<p class="empty">No last_result on status yet.</p>
		{:else}
			<p class="meta" title={`status ${lastSyncResult.status}`}>
				Last result: <code>{lastSyncResult.status}</code> - {lastSyncResult.message}
			</p>
		{/if}
	</section>
</div>

<style>
	.farm-jobs {
		display: flex;
		flex-direction: column;
		gap: 1rem;
	}

	.copy,
	.sub,
	.empty,
	.meta {
		margin: 0;
		color: var(--text-muted, #8a8a8a);
		font-size: 0.9rem;
		line-height: 1.4;
	}

	.kind-block,
	.cloudsync {
		border: 1px solid var(--border-subtle, #333);
		border-radius: 6px;
		padding: 0.75rem 1rem;
	}

	h3,
	h4 {
		margin: 0 0 0.5rem;
	}

	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.85rem;
	}

	th,
	td {
		text-align: left;
		padding: 0.35rem 0.5rem;
		border-bottom: 1px solid var(--border-subtle, #333);
	}

	.times {
		margin-left: 0.35rem;
	}

	.id {
		font-size: 0.75rem;
		word-break: break-all;
	}

	.fatal {
		padding: 0.5rem 0.75rem;
		border: 1px solid #c44;
		color: #f99;
		background: #2a1111;
		border-radius: 4px;
	}
</style>
