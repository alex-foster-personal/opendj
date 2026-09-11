<script lang="ts">
	/**
	 * The lyric job queue (GET /api/v1/lyrics/jobs, newest first) plus a small
	 * queue-a-job form. Honest contract, stated in the copy: jobs run on the
	 * offline stem/alignment farm - queueing RECORDS the request, the daemon
	 * never processes anything itself.
	 */
	import { onMount } from 'svelte';
	import type { LyricJob } from '$lib/api';
	import { fetchLyricJobs, queueLyricJob } from './lyrics-api';

	const KIND_TITLES: Record<LyricJob['kind'], string> = {
		analyze: 'full per-track run: stems if missing, then alignment + witness + ingest',
		lyricsync: 'realign existing lyrics against the audio (stems must already exist)',
		stems: 'separate stems only (the alignment prerequisite)'
	};

	/** Queue refresh cadence: the offline runner drains jobs on minute-ish
	 * timescales, so 10s keeps status honest without hammering the daemon. */
	const POLL_MS = 10_000;
	/** API-side batch cap (routes/lyrics.py rejects larger jobs). */
	const MAX_BATCH = 500;

	let jobs = $state<LyricJob[]>([]);
	let loaded = $state(false);
	let error = $state<string | null>(null);
	let refreshing = false;

	let formKind = $state<LyricJob['kind']>('analyze');
	let formStableIds = $state('');
	let formNote = $state('');
	let queueing = $state(false);
	let queueError = $state<string | null>(null);

	/** Parsed batch: whitespace/comma separated stable_ids, deduped in order. */
	const formIds = $derived(
		Array.from(new Set(formStableIds.split(/[\s,]+/).filter((id) => id !== '')))
	);
	const canQueue = $derived(formIds.length > 0 && formIds.length <= MAX_BATCH && !queueing);

	async function _refresh(): Promise<void> {
		if (refreshing) return;
		refreshing = true;
		try {
			jobs = await fetchLyricJobs();
			loaded = true;
			error = null;
		} catch (exc) {
			error = exc instanceof Error ? exc.message : String(exc);
		} finally {
			refreshing = false;
		}
	}

	onMount(() => {
		void _refresh();
		const timer = setInterval(() => void _refresh(), POLL_MS);
		return () => clearInterval(timer);
	});

	async function _queue(event: SubmitEvent): Promise<void> {
		event.preventDefault();
		if (formIds.length === 0) return;
		queueing = true;
		queueError = null;
		try {
			const job = await queueLyricJob(formKind, formIds, formNote.trim() || undefined);
			jobs = [job, ...jobs];
			formStableIds = '';
			formNote = '';
		} catch (exc) {
			queueError = exc instanceof Error ? exc.message : String(exc);
		} finally {
			queueing = false;
		}
	}
</script>

<div class="jobs">
	<h4>Job queue</h4>
	<p class="copy">
		Runs on the offline stem/alignment farm - queueing records the request; nothing is processed by
		this daemon. The runner scripts drain the queue and ingest results back into state.db.
	</p>

	<form class="queue-form" onsubmit={_queue}>
		<label>
			kind
			<select bind:value={formKind} title={KIND_TITLES[formKind]}>
				{#each Object.entries(KIND_TITLES) as [kind, title] (kind)}
					<option value={kind} {title}>{kind}</option>
				{/each}
			</select>
		</label>
		<label class="grow">
			stable_id(s)
			<textarea
				rows="2"
				bind:value={formStableIds}
				placeholder="one or more stable_ids, whitespace or comma separated"
				spellcheck="false"
				title="Tracks to queue as ONE batch job (max {MAX_BATCH}). All must exist in the tracks table; unknown ids are rejected by the API. Parsed: {formIds.length} id(s)."
			></textarea>
		</label>
		<label class="grow">
			note
			<input
				type="text"
				bind:value={formNote}
				placeholder="optional: why this track"
				title="Optional note stored with the job for the runner and future you."
			/>
		</label>
		<button
			type="submit"
			disabled={!canQueue}
			title={canQueue
				? `POST /api/v1/lyrics/jobs with ${formIds.length} track(s) - records the request for the offline farm`
				: queueing
					? 'request in flight'
					: formIds.length > MAX_BATCH
						? `too many ids: ${formIds.length} > API cap of ${MAX_BATCH}`
						: 'enter at least one stable_id first'}
		>
			{queueing
				? 'Queueing...'
				: formIds.length > 1
					? `Queue job (${formIds.length} tracks)`
					: 'Queue job'}
		</button>
	</form>

	{#if queueError}
		<div class="error">
			QUEUE REJECTED

			{queueError}
		</div>
	{/if}

	{#if error}
		<div class="error">
			LOAD FAILED

			{error}
		</div>
	{:else if !loaded}
		<p class="copy">Loading job queue...</p>
	{:else if jobs.length === 0}
		<p class="copy">Queue is empty - no processing requests recorded yet.</p>
	{:else}
		<ul class="list">
			{#each jobs as job (job.id)}
				<li class="job">
					<span class="status s-{job.status}" title="queued: waiting for the offline runner; done: runner finished and ingested; failed: runner gave up - see its log">
						{job.status}
					</span>
					<span class="kind" title={KIND_TITLES[job.kind]}>{job.kind}</span>
					<span
						class="tracks"
						title="Number of tracks in this job. First ids: {job.stable_ids
							.slice(0, 3)
							.join(', ')}{job.stable_ids.length > 3 ? ', ...' : ''}"
					>
						{job.stable_ids.length} track(s)
					</span>
					<span class="ts" title="When the request was queued (UTC).">{job.ts}</span>
					{#if job.note}
						<span class="note">{job.note}</span>
					{/if}
				</li>
			{/each}
		</ul>
	{/if}
</div>

<style>
	h4 {
		font-size: 0.9rem;
		color: var(--fg);
		margin: 0 0 0.25rem 0;
	}
	.copy {
		color: var(--muted);
		font-size: 0.8rem;
		margin: 0 0 0.6rem 0;
		max-width: 80ch;
	}
	.queue-form {
		display: flex;
		align-items: flex-end;
		gap: 0.6rem;
		flex-wrap: wrap;
		margin-bottom: 0.75rem;
		max-width: 46rem;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		font-size: 0.7rem;
		color: var(--muted);
	}
	label.grow {
		flex: 1 1 12rem;
	}
	select,
	input,
	textarea {
		background: var(--chip-bg);
		color: var(--fg);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 0.3rem 0.5rem;
		font-size: 0.8rem;
	}
	textarea {
		resize: vertical;
		font-family: inherit;
	}
	.queue-form button {
		background: var(--chip-bg);
		color: var(--accent);
		border: 1px solid var(--accent);
		border-radius: 6px;
		padding: 0.3rem 0.8rem;
		font-size: 0.78rem;
		cursor: pointer;
	}
	.queue-form button:disabled {
		opacity: 0.45;
		color: var(--fg);
		border-color: var(--border);
		cursor: default;
	}
	.list {
		list-style: none;
		margin: 0;
		padding: 0;
		max-width: 46rem;
	}
	.job {
		display: flex;
		align-items: baseline;
		gap: 0.6rem;
		flex-wrap: wrap;
		padding: 0.35rem 0.2rem;
		border-bottom: 1px solid var(--border);
		font-size: 0.78rem;
	}
	.status {
		border: 1px solid var(--border);
		border-radius: 999px;
		padding: 0.05rem 0.5rem;
		font-size: 0.68rem;
		cursor: help;
	}
	.status.s-queued {
		color: var(--accent);
		border-color: var(--accent);
	}
	.status.s-done {
		color: var(--kpi-ok);
		border-color: var(--kpi-ok);
	}
	.status.s-failed {
		color: #fff;
		background: var(--danger);
		border-color: var(--danger);
	}
	.kind {
		font-weight: 600;
		cursor: help;
	}
	.tracks {
		font-variant-numeric: tabular-nums;
		cursor: help;
	}
	.ts {
		color: var(--muted);
		font-variant-numeric: tabular-nums;
		font-size: 0.72rem;
		cursor: help;
	}
	.note {
		color: var(--muted);
		font-style: italic;
	}
	.error {
		background: var(--danger);
		color: #fff;
		padding: 0.6rem 0.8rem;
		border-radius: 6px;
		font-size: 0.8rem;
		font-weight: 600;
		white-space: pre-wrap;
		line-height: 1.45;
		margin-bottom: 0.5rem;
		max-width: 40rem;
	}
</style>
