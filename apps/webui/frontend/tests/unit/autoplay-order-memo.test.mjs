/**
 * The AutoPlay poll must not re-simulate the charted order when nothing it
 * depends on has changed.
 *
 * Wed 2 Sep 2026, the maintainer's Chrome review loop: 1.5 fps, two long tasks of ~1.5 s
 * in every 2 s window, all attributed by the Long Animation Frames API to the
 * 250 ms setInterval in auto-play.svelte.ts. _refreshChartedOrder called
 * simulateAutoPlayChain over the open playlist (8558 rows, O(rows^2)) on every
 * tick, with identical inputs each time. Memory and load were green; this was
 * the app.
 *
 * [if] two polls with identical inputs both simulate [then] the main thread is
 *   saturated at 4 simulations a second on a big library - broken.
 * [if] any single input changes and the key does not [then] the order column
 *   goes stale after a playlist edit, a pref flip or a played-set change - broken.
 * [if] _refreshChartedOrder stops consulting the key before simulating [then]
 *   the memo exists but the poll ignores it - broken.
 * [if] simulateAutoPlayChain goes back to playlist.find per step [then] the
 *   one-off simulation is O(rows^2) again - broken.
 * [if] the charted simulation stops being bounded to a constant number of row
 *   touches per chain step [then] every handoff freezes the UI - broken.
 *
 * The scale block used to assert wall-clock (`ms < 750`). That threshold was
 * load-sensitive, not a property of the code: it passed alone and went red
 * whenever a Playwright run or a Vite build shared the machine (Fri 4 Sep 2026,
 * twice, while the same 8558-row walk measured 3623 ms under contention against
 * 77 ms idle). Wall-clock is gone; the same regression is now caught by counting
 * the row property reads the picker performs, which is a fixed number for a
 * given algorithm on a given fixture and does not move with CPU load.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const CONTROLLER_SOURCE = readFileSync(
	fileURLToPath(new URL('../../src/lib/rb/auto-play.svelte.ts', import.meta.url)),
	'utf8'
);
const CHAIN_SOURCE = readFileSync(
	fileURLToPath(new URL('../../src/lib/rb/auto-play-chain.ts', import.meta.url)),
	'utf8'
);

const BASE = Object.freeze({
	feed_epoch: 3,
	playlist_revision: 7,
	source_stable_id: 'sid-a',
	source_key: '8A',
	source_bpm: 124,
	enforce_play_order: false,
	maximize_reach: true,
	min_tempo_ratio: 0.84,
	max_tempo_ratio: 1.16,
	exclude_ids: new Set(['sid-x']),
	played_ids: new Set(['sid-p', 'sid-q']),
	follower_deck: 2,
	follower_pitch_range: 8
});

describe('chartedOrderKey', () => {
	let mod;
	before(async () => {
		mod = await loadTypeScriptModule('src/lib/rb/auto-play.ts');
	});

	it('identical inputs yield an identical key, so a steady poll is a no-op', () => {
		const a = mod.chartedOrderKey(BASE);
		const b = mod.chartedOrderKey({ ...BASE, exclude_ids: new Set(['sid-x']), played_ids: new Set(['sid-q', 'sid-p']) });
		assert.equal(a, b, 'if two polls with identical inputs produce different keys then every tick simulates - broken');
	});

	it('every input the simulation reads changes the key', () => {
		const base = mod.chartedOrderKey(BASE);
		const variants = {
			feed_epoch: { ...BASE, feed_epoch: 4 },
			playlist_revision: { ...BASE, playlist_revision: 8 },
			source_stable_id: { ...BASE, source_stable_id: 'sid-b' },
			source_key: { ...BASE, source_key: '9A' },
			source_bpm: { ...BASE, source_bpm: 126 },
			enforce_play_order: { ...BASE, enforce_play_order: true },
			maximize_reach: { ...BASE, maximize_reach: false },
			min_tempo_ratio: { ...BASE, min_tempo_ratio: 0.9 },
			max_tempo_ratio: { ...BASE, max_tempo_ratio: 1.1 },
			exclude_ids: { ...BASE, exclude_ids: new Set(['sid-x', 'sid-y']) },
			played_ids: { ...BASE, played_ids: new Set(['sid-p']) },
			follower_deck: { ...BASE, follower_deck: 3 },
			follower_pitch_range: { ...BASE, follower_pitch_range: 16 }
		};
		for (const [name, input] of Object.entries(variants)) {
			assert.notEqual(mod.chartedOrderKey(input), base, `if ${name} changes and the key does not then the order column goes stale - broken`);
		}
	});

	it('tracks a playlist-content revision without resetting the membership epoch', () => {
		const feed = [
			{ stable_id: 'sid-a', key: '8A', bpm: 124, file_exists: true },
			{ stable_id: 'sid-b', key: '9A', bpm: 126, file_exists: true }
		];
		mod.setAutoPlayTrackFeed('playlist:a', feed);
		const membershipEpoch = mod.getAutoPlayFeedEpoch();
		const playlistRevision = mod.getAutoPlayPlaylistRevision();
		mod.setAutoPlayTrackFeed('playlist:a', [
			feed[0],
			{ ...feed[1], bpm: 128 }
		]);
		assert.equal(
			mod.getAutoPlayFeedEpoch(),
			membershipEpoch,
			'if a metadata edit resets the membership epoch then played history is lost mid-set - broken'
		);
		assert.equal(
			mod.getAutoPlayPlaylistRevision(),
			playlistRevision + 1,
			'if BPM/key/file changes do not advance the playlist revision then a future live feed leaves the chart stale - broken'
		);
	});
});

describe('the poll consults the memo (source guards; the controller is rune-bound)', () => {
	it('_refreshChartedOrder computes the key and returns before simulating when it is unchanged', () => {
		const start = CONTROLLER_SOURCE.indexOf('function _refreshChartedOrder(');
		const end = CONTROLLER_SOURCE.indexOf('\n}\n', start);
		const body = CONTROLLER_SOURCE.slice(start, end);
		const keyAt = body.indexOf('chartedOrderKey(');
		const guardAt = body.indexOf('if (key === _chartedOrderKey) return;');
		const simAt = body.indexOf('simulateAutoPlayChain(');
		assert.ok(keyAt > 0 && guardAt > keyAt && simAt > guardAt,
			'if _refreshChartedOrder simulates before checking the memo key then the poll ignores the memo - broken');
	});

	it('every published empty order invalidates the memo before the next chart', () => {
		const clearStart = CONTROLLER_SOURCE.indexOf('function _clearChartedOrder(): void {');
		const clearEnd = CONTROLLER_SOURCE.indexOf('\n}', clearStart);
		assert.ok(clearStart >= 0 && clearEnd > clearStart, 'if the clear helper is absent then an empty order can leave its memo live - broken');
		const clear = CONTROLLER_SOURCE.slice(clearStart, clearEnd);
		assert.match(clear, /_chartedOrderKey = null;/);
		assert.match(clear, /publishAutoPlayOrder\(\[\]\);/);

		const refreshStart = CONTROLLER_SOURCE.indexOf('function _refreshChartedOrder(');
		const refresh = CONTROLLER_SOURCE.slice(refreshStart, CONTROLLER_SOURCE.indexOf('\n}\n', refreshStart));
		assert.equal(
			refresh.split('_clearChartedOrder();').length - 1,
			2,
			'if either chart guard publishes an empty order without clearing the memo then an unchanged recovery stays blank - broken'
		);

		const tickStart = CONTROLLER_SOURCE.indexOf('async function _tick(): Promise<void> {');
		const tick = CONTROLLER_SOURCE.slice(tickStart, CONTROLLER_SOURCE.indexOf('function _startPoll(): void {', tickStart));
		assert.equal(
			tick.split('_clearChartedOrder();').length - 1,
			2,
			'if either tick guard clears the order without invalidating the memo then an unchanged recovery stays blank - broken'
		);
		const uninstallStart = CONTROLLER_SOURCE.indexOf('\treturn () => {');
		const uninstall = CONTROLLER_SOURCE.slice(uninstallStart);
		assert.match(uninstall, /_clearChartedOrder\(\);/, 'if teardown clears the order without invalidating the memo then re-entry can stay blank - broken');
	});

	it('the arm watcher clears an activation queue without subscribing to its writes', () => {
		const watcherStart = CONTROLLER_SOURCE.indexOf('$effect(() => {');
		const watcher = CONTROLLER_SOURCE.slice(watcherStart, CONTROLLER_SOURCE.indexOf('\n\t\t});', watcherStart));
		assert.match(
			watcher,
			/clearAutoPlayOrder\(\)/,
			'if the arm watcher reads autoPlayOrder while clearing it then a published queue re-runs the watcher and disappears - broken'
		);
	});

	it('the chart uses the selected follower pitch range, and keys it', () => {
		const start = CONTROLLER_SOURCE.indexOf('function _refreshChartedOrder(');
		const body = CONTROLLER_SOURCE.slice(start, CONTROLLER_SOURCE.indexOf('\n}\n', start));
		assert.match(body, /const follower = pickFollowerDeck\(snaps, source\.id\)/);
		assert.match(body, /const followerPitchRange = pitchRanges\[follower\]/);
		assert.match(body, /tempoBoundsFromPitchRange\(followerPitchRange\)/);
		assert.match(body, /follower_deck: follower/);
		assert.match(body, /follower_pitch_range: followerPitchRange/);
	});

	it('simulateAutoPlayChain indexes the playlist once instead of a linear find per step', () => {
		const start = CHAIN_SOURCE.indexOf('export function simulateAutoPlayChain(');
		const body = CHAIN_SOURCE.slice(start, CHAIN_SOURCE.indexOf('\n}\n', start));
		assert.ok(body.includes('new Map(input.playlist.map('), 'if the chain builds no index then the simulation is O(rows^2) - broken');
		assert.ok(!body.includes('input.playlist.find('), 'if the chain still does playlist.find per step then the index is unused - broken');
	});
});

describe('simulateAutoPlayChain at library scale', () => {
	const keys = ['1A', '2A', '3A', '4A', '5A', '6A', '7A', '8A', '9A', '10A', '11A', '12A'];
	const playlist = Array.from({ length: 8558 }, (_, i) => ({
		stable_id: `sid-${i}`,
		key: keys[i % keys.length],
		bpm: 120 + (i % 20),
		file_exists: true,
		play_order: i
	}));

	/**
	 * COUNTED_ROWS is deliberately smaller than the 8558-row fixture above.
	 *
	 * The work ratio asserted below is scale-invariant once the maximize-reach
	 * budget fallback engages, which it does from 448 rows up (measured by
	 * bisection Fri 4 Sep 2026; reachable * candidates passes
	 * AUTO_PLAY_REACH_MAX_STEPS). Measured
	 * touches per row per chain step, Fri 4 Sep 2026: 4.10 at 1000 rows, 4.15 at
	 * 2000, 4.18 at 4279, 4.19 at 8558. 2000 rows buys the same signal for a
	 * quarter of the work, and a real complexity regression fails in seconds
	 * rather than grinding for minutes before the runner gives up on it.
	 * `falls back to greedy` below pins the branch so shrinking the fixture
	 * cannot silently stop exercising it.
	 */
	const COUNTED_ROWS = 2000;
	const HORIZON = 65;
	/** Row property touches per row per chain step; 4.19 measured, see above. */
	const MAX_TOUCHES_PER_ROW_PER_STEP = 8;

	let chain;
	before(async () => {
		chain = await loadTypeScriptModule('src/lib/rb/auto-play-chain.ts');
	});

	/**
	 * Playlist rows that tally every property read the picker performs, and trip
	 * the moment the tally passes `budget`.
	 *
	 * Tripping inside the getter rather than asserting afterwards is what keeps
	 * the failure legible. Removing the reach budget was measured Fri 4 Sep 2026:
	 * with a post-hoc assert the walk never returned, and the runner killed the
	 * whole file at 60 s with `test timed out` and named no cause. Tripping
	 * mid-walk turned the same mutant into a named failure in 3 s.
	 */
	function countingPlaylist(rows, budget = Number.POSITIVE_INFINITY) {
		const counter = { touches: 0 };
		const bump = () => {
			counter.touches += 1;
			if (counter.touches > budget) {
				throw new Error(
					`if the charted simulation touches more than ${budget} row properties (over ${MAX_TOUCHES_PER_ROW_PER_STEP} per row per chain step) then every handoff freezes the UI - broken`
				);
			}
		};
		const list = Array.from({ length: rows }, (_, i) => {
			const stable_id = `sid-${i}`;
			const key = keys[i % keys.length];
			const bpm = 120 + (i % 20);
			return {
				get stable_id() { bump(); return stable_id; },
				get key() { bump(); return key; },
				get bpm() { bump(); return bpm; },
				get file_exists() { bump(); return true; },
				play_order: i
			};
		});
		return { list, counter };
	}

	it('honors max_chain_length (start row included)', () => {
		const out = chain.simulateAutoPlayChain({
			playlist,
			start_stable_id: 'sid-0',
			enforce_play_order: true,
			maximize_reach: false,
			min_tempo_ratio: 0.84,
			max_tempo_ratio: 1.16,
			max_chain_length: 65
		});
		assert.equal(out.length, 65, 'if the cap is ignored then the column walk is the whole library again - broken');
		assert.equal(out[0], 'sid-0');
	});

	it('maximize_reach falls back to greedy at library scale, which is what keeps the walk linear', () => {
		const { list } = countingPlaylist(COUNTED_ROWS);
		const pick = chain.pickNextMaximizingReach({
			candidates: list.slice(1),
			current_key: list[0].key,
			current_bpm: list[0].bpm,
			min_tempo_ratio: 0.84,
			max_tempo_ratio: 1.16,
			max_steps: chain.AUTO_PLAY_REACH_MAX_STEPS
		});
		assert.equal(pick.fell_back, true,
			`if ${COUNTED_ROWS} rows no longer exhausts the reach budget then the counted test below stopped exercising the branch that bounds the walk - broken`);
	});

	it('the charted horizon touches each row a bounded number of times per step', () => {
		const budget = COUNTED_ROWS * HORIZON * MAX_TOUCHES_PER_ROW_PER_STEP;
		const { list, counter } = countingPlaylist(COUNTED_ROWS, budget);
		const out = chain.simulateAutoPlayChain({
			playlist: list,
			start_stable_id: 'sid-0',
			enforce_play_order: false,
			maximize_reach: true,
			min_tempo_ratio: 0.84,
			max_tempo_ratio: 1.16,
			max_chain_length: HORIZON
		});
		// Guards the denominator: a chain that died at step 2 would pass any
		// budget expressed over the full horizon without doing the work.
		assert.equal(out.length, HORIZON,
			'if the charted chain stops short of the horizon then the budget below is measured against work never done - broken');
		// The getter above trips first on a breach; this states the property in
		// full and covers a budget that is exceeded exactly at the last read.
		const ratio = counter.touches / (COUNTED_ROWS * HORIZON);
		assert.ok(counter.touches <= budget,
			`if the charted simulation touches ${counter.touches} row properties (${ratio.toFixed(1)} per row per step, budget ${MAX_TOUCHES_PER_ROW_PER_STEP}) then every handoff freezes the UI - broken`);
	});

	it('the controller passes the horizon, so the column never walks the whole library', () => {
		assert.ok(
			CONTROLLER_SOURCE.includes('max_chain_length: CHARTED_ORDER_HORIZON + 1'),
			'if _refreshChartedOrder simulates without max_chain_length then a 7 s freeze returns at every handoff - broken'
		);
	});
});
