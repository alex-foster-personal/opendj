<script lang="ts">
	/**
	 * The native-analysis v1 BACKFILL QUEUE panel (NATIVE-10).
	 *
	 * Four controls, and every one of them is the UI leg of a triple the repo's
	 * agent-native parity rule requires: an HTTP endpoint
	 * (/api/v1/analysis/backfill/*) and a CLI subcommand
	 * (`python -m apps.analysis.queue_cli <verb>`) exist for each. A parity test
	 * reads this file and asserts the third leg, so a control added here without
	 * its endpoint and its command fails the suite rather than shipping as a
	 * browser-only flow.
	 *
	 * WHAT THIS PANEL DOES NOT DO: run the analysis. A drain holds a process pool
	 * for minutes to hours, so it is a CLI process, not a request the daemon
	 * serves; a button that started one inside a request worker would block the
	 * event loop and die with the request. The panel PLANS (enqueue), WATCHES
	 * (progress) and STEERS (cancel, resume), and it prints the exact command to
	 * start the drain, selectable, because a control you cannot copy is a control
	 * an operator has to remember.
	 *
	 * NO MOCKED DATA. With no daemon, or with a batch that does not exist, this
	 * renders the error it got. It never renders zeros that look like an empty
	 * queue -- "unknown" and "fine" must not look the same.
	 *
	 * EVERY NUMBER CARRIES A HOVER TITLE (house rule), including the ones whose
	 * meaning looks obvious: "workers 4" is not obvious at all, since the 4 comes
	 * from the memory admission rule's band and never from core count.
	 *
	 * Written INSIDE the script block, not as a leading `<!-- -->`, and the
	 * sibling route page does the same. jscpd 5.0.15 (the duplication gate's
	 * pinned tool) mis-tokenizes an SFC that opens with an HTML comment and
	 * reports the WHOLE script block as a clone of itself: measured on this
	 * file, 135 duplicated lines from one file, and prepending this same
	 * comment to BrowserPanel.svelte reproduces it there at 2522 lines. The
	 * words are unchanged; only where they live is.
	 */
	import { onDestroy } from 'svelte';

	import {
		BAND_LABELS,
		backfillProgress,
		cancelBackfill,
		enqueueBackfill,
		listBackfillBatches,
		resumeBackfill,
		type BackfillBatchSummary,
		type BackfillEnqueueResult,
		type BackfillProgress
	} from '$lib/rb/api-analysis-backfill';

	/** Poll cadence while a batch is watched. A backfill is a background job
	 * measured in minutes, so a two second poll is plenty and costs nothing. */
	const POLL_MS = 2000;

	let stableIdsText = $state('');
	let lane = $state('beatgrid');
	let backend = $state('own_beatgrid.backfill');
	let note = $state('');

	let batches = $state<BackfillBatchSummary[] | null>(null);
	let watched = $state<BackfillProgress | null>(null);
	let watchedId = $state<string | null>(null);
	let lastPlan = $state<BackfillEnqueueResult | null>(null);
	/** The last failure, verbatim. Never cleared by a silent retry. */
	let fault = $state<string | null>(null);
	let busy = $state(false);

	let timer: ReturnType<typeof setInterval> | null = null;

	const parsedIds = $derived(
		stableIdsText
			.split(/[\s,]+/)
			.map((s) => s.trim())
			.filter((s) => s.length > 0)
	);

	const drainCommand = $derived(
		watchedId && watched && watched.items.length > 0
			? `python -m apps.analysis.queue_cli run --batch-id ${watchedId} --backend ${watched.items[0].backend}`
			: watchedId
				? `python -m apps.analysis.queue_cli run --batch-id ${watchedId} --backend ${backend}`
				: null
	);

	function fail(err: unknown): void {
		fault = err instanceof Error ? `${err.name}: ${err.message}` : String(err);
	}

	async function refreshBatches(): Promise<void> {
		try {
			batches = await listBackfillBatches();
			fault = null;
		} catch (err) {
			fail(err);
		}
	}

	async function refreshWatched(): Promise<void> {
		if (watchedId === null) return;
		try {
			watched = await backfillProgress(watchedId);
			fault = null;
		} catch (err) {
			fail(err);
		}
	}

	function watch(batchId: string): void {
		watchedId = batchId;
		watched = null;
		void refreshWatched();
		if (timer === null) timer = setInterval(() => void refreshWatched(), POLL_MS);
	}

	async function onEnqueue(): Promise<void> {
		if (parsedIds.length === 0) {
			fault = 'enqueue needs at least one stable_id';
			return;
		}
		busy = true;
		try {
			lastPlan = await enqueueBackfill({
				stableIds: parsedIds,
				lane,
				backend,
				note: note.trim() || undefined
			});
			fault = null;
			watch(lastPlan.batch_id);
			await refreshBatches();
		} catch (err) {
			fail(err);
		} finally {
			busy = false;
		}
	}

	async function onCancel(): Promise<void> {
		if (watchedId === null) return;
		busy = true;
		try {
			await cancelBackfill(watchedId);
			fault = null;
			await refreshWatched();
			await refreshBatches();
		} catch (err) {
			fail(err);
		} finally {
			busy = false;
		}
	}

	async function onResume(): Promise<void> {
		if (watchedId === null) return;
		busy = true;
		try {
			await resumeBackfill(watchedId);
			fault = null;
			await refreshWatched();
			await refreshBatches();
		} catch (err) {
			fail(err);
		} finally {
			busy = false;
		}
	}

	onDestroy(() => {
		if (timer !== null) clearInterval(timer);
		timer = null;
	});

	void refreshBatches();
</script>

<section class="panel">
	<header>
		<h2>Analysis backfill queue</h2>
		<p class="sub">
			Plan, watch and steer the native-analysis v1 backfill. Concurrency is chosen by the
			memory admission rule from the longest admitted track, never from core count.
		</p>
	</header>

	{#if fault}
		<p class="fault" role="alert">{fault}</p>
	{/if}

	<fieldset>
		<legend>Enqueue</legend>
		<label>
			stable_ids
			<textarea
				bind:value={stableIdsText}
				rows="3"
				placeholder="one per line, or comma separated"
			></textarea>
		</label>
		<div class="row">
			<label>
				lane
				<select bind:value={lane}>
					<option value="beatgrid">beatgrid</option>
					<option value="key">key</option>
					<option value="waveform">waveform</option>
					<option value="loudness">loudness</option>
					<option value="vocal">vocal</option>
				</select>
			</label>
			<label>
				backend
				<input bind:value={backend} />
			</label>
			<label>
				note
				<input bind:value={note} placeholder="why this batch exists" />
			</label>
		</div>
		<p>
			<span class="num" title="How many stable_ids the box currently holds. This is what will be OFFERED to the admission rule, not what will be admitted.">{parsedIds.length}</span>
			offered
			<button onclick={onEnqueue} disabled={busy || parsedIds.length === 0}>Enqueue</button>
		</p>
	</fieldset>

	{#if lastPlan}
		<div class="plan">
			<h3>Plan for {lastPlan.batch_id}</h3>
			<p>
				<span class="num" title="Tracks the memory admission rule accepted: predicted peak RSS (floor + slope x audio-minutes) fits the 6.5 GB per-worker cap and the track is under 90 minutes.">{lastPlan.admitted}</span>
				admitted,
				<span class="num" title="Tracks refused, each with a named reason in the item list below. Refused tracks are NEVER folded into a coverage percentage.">{lastPlan.refused}</span>
				refused,
				<span class="num" title="Worker processes this batch will run at, chosen from the LONGEST ADMITTED track: 4 under 20 min, 2 from 20 to 45 min, 1 above 45 min. Never from core count.">{lastPlan.workers}</span>
				workers
			</p>
			<p class="model" title="The measured peak-RSS model this batch was budgeted under. Floor and slope are measurements of one producer version, not constants.">
				band <code>{lastPlan.band}</code> ({BAND_LABELS[lastPlan.band]}); budgeted at
				<span class="num" title="Predicted peak RSS at zero audio length, in MB, for this producer.">{lastPlan.memory_model.floor_mb}</span>
				MB +
				<span class="num" title="Extra predicted peak RSS per audio minute, in MB, for this producer.">{lastPlan.memory_model.slope_mb_per_min}</span>
				MB/min, measured: {lastPlan.memory_model.measured_on}
			</p>
		</div>
	{/if}

	{#if drainCommand}
		<p class="drain">
			Start the drain (the panel never runs it: a drain holds a process pool for minutes to
			hours):
			<code>{drainCommand}</code>
		</p>
	{/if}

	{#if watched}
		<div class="watched">
			<h3>
				{watched.batch_id} <code>{watched.state}</code>
			</h3>
			<p>
				<span class="num" title="Items in a terminal state: done, skipped, failed or refused. A resume never revisits these.">{watched.settled}</span>
				of
				<span class="num" title="Every item in the batch, admitted and refused alike.">{watched.total}</span>
				settled;
				<span class="num" title="Worker processes the admission rule sized this batch at.">{watched.workers}</span>
				workers, band <code>{watched.band}</code>
			</p>
			<ul class="counts">
				{#each Object.entries(watched.counts) as [state, count] (state)}
					<li>
						{state}
						<span class="num" title={`Items currently in the ${state} state.`}>{count}</span>
					</li>
				{/each}
			</ul>
			<div class="row">
				<button onclick={onCancel} disabled={busy}>Cancel</button>
				<button onclick={onResume} disabled={busy}>Resume</button>
				<button onclick={refreshWatched} disabled={busy}>Refresh</button>
			</div>
			<table>
				<thead>
					<tr><th>track</th><th>lane</th><th>state</th><th>attempts</th><th>reason</th></tr>
				</thead>
				<tbody>
					{#each watched.items as item (item.stable_id + item.lane)}
						<tr>
							<td><code>{item.stable_id}</code></td>
							<td>{item.lane}</td>
							<td>{item.state}</td>
							<td>
								<span class="num" title="How many times a runner has claimed this item. A kill-and-resume shows 2.">{item.attempts}</span>
							</td>
							<td class="reason">{item.reason ?? ''}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}

	<div class="batches">
		<h3>Recent batches</h3>
		{#if batches === null}
			<p>not loaded</p>
		{:else if batches.length === 0}
			<p>no batches yet</p>
		{:else}
			<ul>
				{#each batches as b (b.batch_id)}
					<li>
						<button onclick={() => watch(b.batch_id)}>{b.batch_id}</button>
						<code>{b.state}</code>
						<span class="num" title="Worker processes the admission rule sized this batch at.">{b.workers}</span>
						workers, {b.created_at}{b.note ? ` -- ${b.note}` : ''}
					</li>
				{/each}
			</ul>
		{/if}
	</div>
</section>

<style>
	.panel {
		display: flex;
		flex-direction: column;
		gap: 1rem;
		padding: 1rem;
		font-size: 0.9rem;
	}
	.sub {
		color: var(--text-muted, #888);
		margin: 0.25rem 0 0;
	}
	.fault {
		border: 1px solid #b00;
		padding: 0.5rem;
		color: #b00;
		white-space: pre-wrap;
	}
	fieldset {
		border: 1px solid var(--border, #444);
		padding: 0.75rem;
	}
	.row {
		display: flex;
		gap: 0.75rem;
		flex-wrap: wrap;
		align-items: flex-end;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
	}
	textarea,
	input,
	select {
		font-family: inherit;
	}
	.num {
		font-variant-numeric: tabular-nums;
		font-weight: 600;
		cursor: help;
		border-bottom: 1px dotted currentColor;
	}
	.counts {
		display: flex;
		gap: 0.75rem;
		flex-wrap: wrap;
		list-style: none;
		padding: 0;
	}
	table {
		border-collapse: collapse;
		width: 100%;
	}
	th,
	td {
		border-bottom: 1px solid var(--border, #333);
		text-align: left;
		padding: 0.2rem 0.4rem;
	}
	.reason {
		font-size: 0.8rem;
		color: var(--text-muted, #999);
	}
	.batches ul {
		list-style: none;
		padding: 0;
	}
</style>
