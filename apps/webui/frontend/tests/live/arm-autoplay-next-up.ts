/**
 * Arm AutoPlay against a running engine and wait until that engine stores the
 * charted head. Production modules only: the browser feed, the arm the
 * controller runs when AutoPlay is enabled, and the chart publish that sends
 * PUT /api/v1/ahead-analysis/next-up.
 *
 * Only the confirmed leader tab may write next-up (AGENT-18), so this client
 * binds a leadership the way /performance does: the shipped controller
 * (`createTabLeadership`), confirmed with the shipped `noteLeaseAccepted()`
 * and bound with the shipped `bindNextUpLeadership`. It does NOT win that
 * confirmation with its own leased mirror PUT, as the page would: the live
 * test's MirrorPage already plays the page's mirror (playing deck, xrun
 * counter and detector state) and holds the live lease, and a second mirror
 * writer here would replace the very document that test controls. So this
 * client writes under that page's client id (`writerId`), as the page's own
 * next-up client does. No locks: one Node process is one tab.
 */
import { api, unwrap } from '$lib/api';
import { setAutoPlayTrackFeed, type AutoPlayTrackRow } from '$lib/rb/auto-play';
import { NEXT_UP_COUNT } from '$lib/rb/autoplay-next-up';
import { bindNextUpLeadership } from '$lib/rb/autoplay-next-up-lazy';
import { confirmedLeadership, createTabLeadership } from '$lib/rb/tab-leadership';
import {
	activateAutoPlayQueue,
	autoPlayQueue,
	publishAutoPlayOrder
} from '$lib/rb/autoplay-queue.svelte';

const ARM_BUDGET_MS = 10_000;

function chartedHead(chart: readonly string[]): string[] {
	return chart.slice(0, NEXT_UP_COUNT);
}

function readNextUp(coverage: { [key: string]: unknown }): unknown {
	const whilePlaying = coverage['while_playing'];
	if (whilePlaying === null || typeof whilePlaying !== 'object') return null;
	return (whilePlaying as { next_up?: unknown }).next_up ?? null;
}

/** Arm the queue, publish the chart, and return the head the engine stored. */
export async function armChartedNextUp(
	feedIds: readonly string[],
	chart: readonly string[],
	writerId: string
): Promise<string[]> {
	if (chart.length === 0) throw new Error('charted AutoPlay chain is empty');
	const rows: AutoPlayTrackRow[] = feedIds.map((stable_id) => ({
		stable_id,
		key: null,
		bpm: null,
		file_exists: true,
		title: null,
		artist: null
	}));
	const leadership = createTabLeadership({ locks: null });
	leadership.noteLeaseAccepted();
	if (!leadership.isConfirmedLeader()) throw new Error('the live AutoPlay client is not the confirmed leader');
	bindNextUpLeadership(confirmedLeadership(leadership), writerId);
	setAutoPlayTrackFeed('ahead-live-engine', rows);
	activateAutoPlayQueue();
	if (!autoPlayQueue.active) throw new Error('AutoPlay queue did not arm');
	publishAutoPlayOrder(chart);
	const want = chartedHead(chart);
	const deadline = Date.now() + ARM_BUDGET_MS;
	let last: unknown = null;
	while (Date.now() < deadline) {
		const coverage = await unwrap(api.GET('/api/v1/ahead-analysis/coverage'));
		last = readNextUp(coverage);
		if (JSON.stringify(last) === JSON.stringify(want)) return want;
		await new Promise((resolve) => setTimeout(resolve, 50));
	}
	throw new Error(
		`engine next_up never stored the charted head ${JSON.stringify(want)} (last ${JSON.stringify(last)})`
	);
}
