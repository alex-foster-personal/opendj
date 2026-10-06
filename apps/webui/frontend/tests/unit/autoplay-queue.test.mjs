/**
 * PLAY-05: the user-visible queue is created with AutoPlay and must expose
 * exactly the same frozen-plan rows that the handoff controller will use.
 *
 * [if] activation creates no queue state [then] a user cannot inspect the
 *   planned handoffs before the first transfer - broken.
 * [if] queue rows are rebuilt from the live browser view [then] a re-sort can
 *   show a different order from the frozen AutoPlay snapshot - broken.
 * [if] a planned stable id lacks its snapshot row [then] silently omitting it
 *   makes the displayed queue disagree with playback - broken.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { installTimerProbe, loadRuneModule } from './load-rune-module.mjs';

let autoPlay;
const CONTROLLER_SOURCE = readFileSync(
	fileURLToPath(new URL('../../src/lib/rb/auto-play.svelte.ts', import.meta.url)),
	'utf8'
);
const QUEUE_SOURCE = readFileSync(
	fileURLToPath(new URL('../../src/lib/rb/autoplay-queue.svelte.ts', import.meta.url)),
	'utf8'
);
const CHART_ORDER_SOURCE = readFileSync(
	fileURLToPath(new URL('../../src/lib/rb/auto-play-chart-order.ts', import.meta.url)),
	'utf8'
);
const TABLE_SOURCE = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)),
	'utf8'
);

before(async () => {
	autoPlay = await loadTypeScriptModule('src/lib/rb/auto-play.ts');
});

describe('PLAY-05 AutoPlay queue entries', () => {
	it('keeps the frozen handoff order and display metadata together', () => {
		const entries = autoPlay.queueEntriesForChain(
			['next-b', 'next-a'],
			[
				{
					stable_id: 'next-a',
					key: '8A',
					bpm: 124,
					file_exists: true,
					title: 'Second in snapshot',
					artist: 'Artist A'
				},
				{
					stable_id: 'next-b',
					key: '9A',
					bpm: 126,
					file_exists: true,
					title: 'First in plan',
					artist: 'Artist B'
				}
			]
		);

		assert.deepEqual(entries, [
			{ stable_id: 'next-b', title: 'First in plan', artist: 'Artist B' },
			{ stable_id: 'next-a', title: 'Second in snapshot', artist: 'Artist A' }
		]);
	});

	it('fails loudly when a planned handoff has no frozen snapshot row', () => {
		assert.throws(
			() =>
				autoPlay.queueEntriesForChain(['missing'], [
					{ stable_id: 'present', key: null, bpm: null, file_exists: true }
				]),
			/autoplay queue: planned stable_id missing is absent from frozen feed/
		);
	});

	it('activates the queue with AutoPlay and exposes the exact controller plan in the table', () => {
		assert.ok(
			CONTROLLER_SOURCE.includes('activateAutoPlayQueue();'),
			'if AutoPlay activation does not create queue state then the queue starts too late - broken'
		);
		// The chart/publish step was extracted to auto-play-chart-order.ts on
		// Sat 12 Sep 2026 (commit 8e897c648, issue #2069 silence-dropout work),
		// so the controller now wires the extraction in rather than publishing
		// inline. Both halves of that wiring are checked here: the controller
		// calls into the extracted module, and the extracted module is the one
		// still handing the queue module its exact planned chain.
		assert.ok(
			/import \{[^}]*\brefreshChartedAutoPlayOrder\b[^}]*\} from '\$lib\/rb\/auto-play-chart-order';/.test(
				CONTROLLER_SOURCE
			) && CONTROLLER_SOURCE.includes('refreshChartedAutoPlayOrder({'),
			'if the controller does not wire in the charted-order refresh then queue and playback can disagree - broken'
		);
		assert.ok(
			CHART_ORDER_SOURCE.includes('publishAutoPlayOrder(full.slice(1));'),
			'if the chart-order module does not publish its exact planned chain then queue and playback can disagree - broken'
		);
		assert.ok(
			QUEUE_SOURCE.includes('queueEntriesForChain(chain, getAutoPlayPlaylist())'),
			'if queue rows are not built from the frozen feed then a re-sort can reorder the displayed plan - broken'
		);
		assert.ok(
			TABLE_SOURCE.includes('data-autoplay-queue-active={autoPlayQueue.active}'),
			'if the track table does not expose the active queue then users cannot inspect it - broken'
		);
	});

	it('renders the frozen queue entries in the header viewer', () => {
		assert.ok(
			TABLE_SOURCE.includes('queue={autoPlayQueue.entries}'),
			'if the queue viewer receives no frozen entries then filtering live rows hides the published plan - broken'
		);
	});
});

/**
 * PLAY-05: the published queue must be simulated with the SAME follower deck
 * and pitch range the handoff will use.
 *
 * _refreshChartedOrder charted with pitchRanges[source.id] while _tick picks
 * with pitchRanges[follower]. A source on WIDE and a free follower on +-8%
 * therefore advertised a next track that playback would refuse: the queue said
 * one thing, the deck would have done another. autoplay-order-memo.test.mjs
 * pins the shape of the fix; this runs the real controller and pins the
 * behavior it was made for.
 *
 * [if] the chart simulates with the source deck's range [then] the advertised
 *   queue contradicts the handoff whenever the decks differ - broken.
 * [if] the queue rows lose their frozen title/artist [then] the viewer shows
 *   unreadable ids - broken.
 */
describe('PLAY-05 queue agrees with the handoff pitch range', () => {
	const RUNE_ENTRY = [
		"export { installAutoPlay } from '$lib/rb/auto-play.svelte';",
		"export { autoPlayOrder, autoPlayQueue } from '$lib/rb/autoplay-queue.svelte';",
		"export { deckStates, pitchRanges } from '$lib/rb/audio-engine.svelte';",
		"export { setAutoPlayTrackFeed } from '$lib/rb/auto-play';",
		"export { setAutoPlayEnforceOrder, setAutoPlayMaximizeReach } from '$lib/rb/prefs.svelte';"
	].join('\n');

	// Source is 124 BPM / 8A on deck 1 at +-16%; deck 2 is free at +-8%.
	// 'wide-only' sits at a 1.12 tempo ratio (inside the source range, outside
	// the follower's); 'both-ok' at 1.05 is inside both. Greedy takes the
	// earliest compatible row, so the two ranges disagree about the very next
	// handoff - which is the whole bug.
	const FEED = [
		{ stable_id: 'src', key: '8A', bpm: 124, file_exists: true, title: 'Source', artist: 'S' },
		{
			stable_id: 'wide-only',
			key: '9A',
			bpm: 138.9,
			file_exists: true,
			title: 'Wide only',
			artist: 'W'
		},
		{ stable_id: 'both-ok', key: '8A', bpm: 130.2, file_exists: true, title: 'Both ok', artist: 'B' }
	];

	it('RUNNING it: charts inside the free follower deck range, not the source range', async () => {
		const probe = installTimerProbe();
		let uninstall = null;
		try {
			const controller = await loadRuneModule(RUNE_ENTRY);
			controller.setAutoPlayEnforceOrder(false);
			controller.setAutoPlayMaximizeReach(false);
			controller.pitchRanges[1] = 16;
			controller.pitchRanges[2] = 8;
			Object.assign(controller.deckStates[1], {
				stable_id: 'src',
				playing: true,
				is_master: true,
				position_ms: 0,
				duration_ms: 300_000,
				key: '8A',
				bpm: 124
			});
			controller.setAutoPlayTrackFeed('queue-pitch-range', FEED);

			uninstall = controller.installAutoPlay();
			const deadline = Date.now() + 8_000;
			while (controller.autoPlayOrder.chain.length === 0 && Date.now() < deadline) {
				await probe.flush();
			}

			assert.equal(
				controller.autoPlayOrder.chain[0],
				'both-ok',
				'if the chart uses the source range then it advertises wide-only, which the +-8% follower would never load - broken'
			);
			// 'wide-only' is still reachable LATER, from 'both-ok' at a 1.067
			// ratio - what the follower range changes is which one comes next.
			assert.deepEqual(
				controller.autoPlayQueue.entries.map((entry) => entry.title),
				['Both ok', 'Wide only'],
				'and the user-viewable queue carries those rows in plan order, with their frozen metadata - broken'
			);
		} finally {
			if (uninstall !== null) uninstall();
			probe.restore();
		}
	});
});
