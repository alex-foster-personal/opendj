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
	import BackfillQueuePanelView from './BackfillQueuePanelView.svelte';

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
				note: note.trim() || undefined,
				drain: false
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

<BackfillQueuePanelView
	bind:stableIdsText
	bind:lane
	bind:backend
	bind:note
	{parsedIds}
	{batches}
	{watched}
	{lastPlan}
	{fault}
	{busy}
	{drainCommand}
	bandLabels={BAND_LABELS}
	{onEnqueue}
	{onCancel}
	{onResume}
	{refreshWatched}
	{watch}
/>
