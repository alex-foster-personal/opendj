import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * AutoPlay candidate order = a snapshot of the user's SORTED view, taken when
 * AutoPlay is switched on.
 *
 * Activating AutoPlay captures the current sorted order; re-activation
 * after sorting captures the new order.
 *
 * [if] sorting then activating does not drive candidates in the sorted order
 *   [then] the user's sort is ignored, which is the original bug - broken.
 * [if] sorting WHILE AutoPlay runs re-orders the set [then] the running set
 *   re-orders under the user's hands mid-track - broken.
 * [if] toggling off then on after a sort does NOT adopt the new order [then]
 *   the user has no way to re-aim AutoPlay at all - broken.
 */

let autoPlay;

before(async () => {
	autoPlay = await loadTypeScriptModule('src/lib/rb/auto-play.ts');
});

const row = (stable_id, key, bpm) => ({ stable_id, key, bpm, file_exists: true });

/** One key and a tight BPM spread, so every track is compatible with every
 * other and the ONLY thing deciding which plays next is membership order.
 * That is what makes these assertions about ORDERING rather than about key
 * compatibility, which auto-play.test.mjs already covers. */
const BY_TITLE = [row('alpha', '8A', 124), row('bravo', '8A', 125), row('charlie', '8A', 126)];
const BY_BPM = [row('charlie', '8A', 126), row('bravo', '8A', 125), row('alpha', '8A', 124)];

/** Ids in the order AutoPlay would actually walk them from `start`. */
function chainFrom(start) {
	const { simulateAutoPlayChain, getAutoPlayPlaylist } = autoPlay;
	return [
		...simulateAutoPlayChain({
			playlist: getAutoPlayPlaylist(),
			start_stable_id: start,
			enforce_play_order: false,
			maximize_reach: false,
			min_tempo_ratio: 0.84,
			max_tempo_ratio: 1.16
		})
	];
}

/** Drives the snapshot the way BrowserPanel's $effect does: every observation
 * of (enabled, current view) goes through step(), and only a non-null publish
 * reaches the real feed.
 *
 * HOSTILE-EMPTY DEFAULT (fixture rule, Tue 1 Sep 2026): the harness starts
 * with an EMPTY, un-hydrated view - the real state at mount - and every test
 * that wants rows must populate explicitly. The previous default of a
 * populated view made the mount race inexpressible, which is exactly where
 * the empty-feed freeze hid. */
function makeHarness(initialView = [], initialScope = 'playlist-a') {
	const snapshot = autoPlay.createAutoPlayFeedSnapshot();
	let enabled = false;
	let view = initialView;
	let scope = initialScope;
	let membershipHydrating = false;
	const apply = () => {
		const decision = snapshot.step(enabled, scope, view, '', membershipHydrating);
		if (decision.publish !== null) autoPlay.setAutoPlayTrackFeed(scope, decision.publish);
		return decision;
	};
	return {
		snapshot,
		setMembershipHydrating(next) {
			membershipHydrating = next;
		},
		sort(next) {
			view = next;
			return apply();
		},
		switchPlaylist(nextScope, nextView) {
			scope = nextScope;
			view = nextView;
			return apply();
		},
		setEnabled(next) {
			enabled = next;
			return apply();
		},
		filter(next, filterKey) {
			view = next;
			const decision = snapshot.step(enabled, scope, view, filterKey, membershipHydrating);
			if (decision.publish !== null) autoPlay.setAutoPlayTrackFeed(scope, decision.publish);
			return decision;
		},
		apply
	};
}

describe('createAutoPlayFeedSnapshot: activation semantics', () => {
	it('(a) sort THEN activate - the sorted order drives candidates', () => {
		const h = makeHarness();
		h.sort(BY_BPM);
		const decision = h.setEnabled(true);

		assert.equal(decision.snapshotted, true);
		assert.deepEqual(
			decision.publish.map((r) => r.stable_id),
			['charlie', 'bravo', 'alpha']
		);
		assert.deepEqual(chainFrom('charlie'), ['charlie', 'bravo', 'alpha']);
	});

	it('(b) activate THEN sort - the running order is unchanged', () => {
		const h = makeHarness(BY_TITLE); // populated world, explicitly
		h.setEnabled(true);
		assert.deepEqual(chainFrom('alpha'), ['alpha', 'bravo', 'charlie']);

		const decision = h.sort(BY_BPM);

		assert.equal(decision.publish, null, 'sorting mid-set must publish nothing');
		assert.equal(decision.snapshotted, false);
		assert.deepEqual(
			chainFrom('alpha'),
			['alpha', 'bravo', 'charlie'],
			'the set in flight must keep the order it was activated with'
		);
	});

	it('switching playlists while enabled restarts from the newly visible playlist', () => {
		const h = makeHarness(BY_TITLE, 'playlist-alpha');
		h.setEnabled(true);
		assert.deepEqual(chainFrom('alpha'), ['alpha', 'bravo', 'charlie']);

		const clearing = h.switchPlaylist('playlist-beta', []);
		assert.equal(clearing.snapshotted, false);
		assert.deepEqual(clearing.publish, [], 'the old feed must retire before the new playlist loads');
		assert.equal(h.snapshot.active, false, 'an empty loading view cannot become the new snapshot');

		const decision = h.sort(BY_BPM);

		assert.equal(decision.snapshotted, true);
		assert.deepEqual(
			decision.publish.map((r) => r.stable_id),
			['charlie', 'bravo', 'alpha'],
			'a playlist switch must replace the old candidate feed immediately'
		);
		assert.deepEqual(chainFrom('charlie'), ['charlie', 'bravo', 'alpha']);
	});

	it('(c) off THEN on after a sort - the new order is adopted', () => {
		const h = makeHarness(BY_TITLE);
		h.setEnabled(true);
		h.sort(BY_BPM); // ignored while running, per (b)
		assert.deepEqual(chainFrom('alpha'), ['alpha', 'bravo', 'charlie']);

		h.setEnabled(false);
		const decision = h.setEnabled(true);

		assert.equal(decision.snapshotted, true);
		assert.deepEqual(chainFrom('charlie'), ['charlie', 'bravo', 'alpha']);
	});

	it('repeated sorts mid-set never publish, however many arrive', () => {
		const h = makeHarness(BY_TITLE);
		h.setEnabled(true);
		for (const next of [BY_BPM, BY_TITLE, BY_BPM, BY_TITLE]) {
			assert.equal(h.sort(next).publish, null);
		}
		assert.deepEqual(chainFrom('alpha'), ['alpha', 'bravo', 'charlie']);
	});

	it('a filter change replaces the frozen candidates, then clearing it restores the full list', () => {
		const h = makeHarness(BY_TITLE);
		h.setEnabled(true);
		const fullEpoch = autoPlay.getAutoPlayFeedEpoch();

		const filtered = h.filter([BY_TITLE[1]], 'search:bravo');
		assert.deepEqual(
			filtered.publish.map((r) => r.stable_id),
			['bravo'],
			'if an active filter does not publish its visible candidates then AutoPlay can play filtered-out tracks - broken'
		);
		assert.equal(filtered.snapshotted, false, 'a filter update is not a new activation');
		assert.deepEqual([...autoPlay.getAutoPlayPlaylist()].map((r) => r.stable_id), ['bravo']);
		assert.notEqual(
			autoPlay.getAutoPlayFeedEpoch(),
			fullEpoch,
			'if a filter change keeps the feed epoch then the chart memo can serve a stale chain - broken'
		);

		const cleared = h.filter(BY_TITLE, '');
		assert.deepEqual(
			cleared.publish.map((r) => r.stable_id),
			['alpha', 'bravo', 'charlie'],
			'if clearing a filter does not publish the full view then AutoPlay stays scoped until reload - broken'
		);
	});

	it('a manually loaded middle track is the chain start, never the top row', () => {
		const h = makeHarness(BY_TITLE);
		h.setEnabled(true);
		assert.deepEqual(
			[
				...autoPlay.simulateAutoPlayChain({
					playlist: autoPlay.getAutoPlayPlaylist(),
					start_stable_id: 'bravo',
					enforce_play_order: true,
					maximize_reach: false,
					min_tempo_ratio: 0.84,
					max_tempo_ratio: 1.16
				})
			],
			['bravo', 'charlie'],
			'if a manual load starts at alpha then AutoPlay re-derives from the list top - broken'
		);
	});

	it('while off, the feed tracks the live view so activation is a plain copy', () => {
		const h = makeHarness();
		assert.deepEqual(
			h.sort(BY_BPM).publish.map((r) => r.stable_id),
			['charlie', 'bravo', 'alpha']
		);
		assert.deepEqual(
			h.sort(BY_TITLE).publish.map((r) => r.stable_id),
			['alpha', 'bravo', 'charlie']
		);
		assert.equal(h.snapshot.active, false);
	});

	it('reports active only while a snapshot is frozen', () => {
		const h = makeHarness(BY_TITLE);
		assert.equal(h.snapshot.active, false);
		h.setEnabled(true);
		assert.equal(h.snapshot.active, true);
		h.setEnabled(false);
		assert.equal(h.snapshot.active, false);
	});

	it('reports when the visible order differs from the activation snapshot', () => {
		const h = makeHarness(BY_TITLE);
		assert.equal(h.snapshot.matches(BY_TITLE), false, 'no snapshot exists before activation');

		h.setEnabled(true);
		assert.equal(h.snapshot.matches(BY_TITLE), true);

		const sortedWhileRunning = h.sort(BY_BPM);
		assert.equal(sortedWhileRunning.publish, null, 'the running feed must remain frozen');
		assert.equal(h.snapshot.matches(BY_BPM), false, 'the new sort must be reported as different');
		assert.equal(h.snapshot.matches(BY_TITLE), true, 'returning to the captured order must match');

		h.setEnabled(false);
		assert.equal(h.snapshot.matches(BY_BPM), false, 'turning AutoPlay off clears the snapshot');
		h.setEnabled(true);
		assert.equal(h.snapshot.matches(BY_BPM), true, 're-activation captures the current order');
	});

	it('(d) enabled at mount with an EMPTY view - does not arm, first rows snapshot', () => {
		// If enabled before the first rows arrive, the panel must not freeze
		// an empty feed or report a key/BPM dead end for the later rows.
		const h = makeHarness(); // hostile-empty default IS the un-hydrated pane
		const atMount = h.setEnabled(true); // observes (enabled, [])

		assert.equal(atMount.snapshotted, false, 'an empty view must not arm the snapshot');
		assert.equal(h.snapshot.active, false);
		assert.deepEqual(atMount.publish, [], 'the live (empty) view still tracks through');

		const hydrated = h.sort(BY_BPM); // rows arrive while already enabled
		assert.equal(hydrated.snapshotted, true, 'first observation WITH rows takes the snapshot');
		assert.equal(h.snapshot.active, true);
		assert.deepEqual(
			hydrated.publish.map((r) => r.stable_id),
			['charlie', 'bravo', 'alpha']
		);
		assert.deepEqual(chainFrom('charlie'), ['charlie', 'bravo', 'alpha']);

		// And the frozen-set rule holds from that point on, exactly as in (b).
		assert.equal(h.sort(BY_TITLE).publish, null, 'later sorts must publish nothing');
	});

	it('(d2) empty view repeats while enabled - keeps waiting, never half-arms', () => {
		const h = makeHarness(); // hostile-empty default: zero rows at activation
		h.setEnabled(true);
		for (let i = 0; i < 3; i++) {
			const again = h.sort([]);
			assert.equal(again.snapshotted, false);
			assert.deepEqual(again.publish, []);
		}
		assert.equal(h.snapshot.active, false);
		assert.equal(h.sort(BY_TITLE).snapshotted, true);
	});

	it('re-activating with an UNCHANGED view still snapshots, and keeps epoch', () => {
		const h = makeHarness(BY_TITLE);
		h.setEnabled(true);
		const epoch = autoPlay.getAutoPlayFeedEpoch();
		h.setEnabled(false);
		assert.equal(h.setEnabled(true).snapshotted, true);
		// Same membership identity, so played history must survive the toggle.
		assert.equal(autoPlay.getAutoPlayFeedEpoch(), epoch);
	});

	it('snapshots the ORDER, not a live reference to the caller array', () => {
		const h = makeHarness();
		const mutable = [...BY_TITLE];
		const decision = h.snapshot.step(true, 'playlist-a', mutable);
		mutable.reverse();
		assert.deepEqual(
			decision.publish.map((r) => r.stable_id),
			['alpha', 'bravo', 'charlie'],
			'a caller reordering its own array must not reorder a taken snapshot'
		);
	});

	it('two snapshots are independent, so panes cannot fight over one state', () => {
		const a = autoPlay.createAutoPlayFeedSnapshot();
		const b = autoPlay.createAutoPlayFeedSnapshot();
		a.step(true, 'playlist-a', BY_TITLE);
		assert.equal(a.active, true);
		assert.equal(b.active, false);
		assert.equal(b.step(true, 'playlist-b', BY_BPM).snapshotted, true);
	});

	it('no interleaving of (enable, hydrate, re-render) can arm on an empty view', () => {
		// Order-independence, asserted rather than argued.
		//
		// The mount race is a question about WHEN BrowserPanel's $effect first
		// observes (auto_play_enabled, visibleRows) relative to preference
		// initialization and the rows fetch. A unit test cannot mount the real
		// component to control that order. It does not have to: every order the
		// component could produce is enumerated here, so the incident no longer
		// depends on the component's timing at all. That is the property the
		// fix actually bought, and it is the one worth guarding.
		//
		// The alphabet is the three things the $effect can observe change:
		//   E = auto_play_enabled flips true (preferences finish loading)
		//   H = the rows fetch resolves (the view hydrates)
		//   R = a re-render with nothing changed (Svelte may re-run an effect
		//       for the same state; the snapshot must be idempotent under it)
		const STEPS = ['E', 'H', 'R'];
		const orders = [];
		for (const a of STEPS)
			for (const b of STEPS)
				for (const c of STEPS)
					for (const d of STEPS) orders.push([a, b, c, d]);

		let armedWithRows = 0;
		for (const order of orders) {
			const h = makeHarness(); // empty view, preferences not yet applied
			let hydrated = false;
			for (const step of order) {
				let decision;
				if (step === 'E') decision = h.setEnabled(true);
				else if (step === 'H') {
					hydrated = true;
					decision = h.sort(BY_BPM);
				} else decision = h.apply();

				if (!decision.snapshotted) continue;
				assert.ok(hydrated, `armed on an empty view under order ${order.join('')}`);
				assert.ok(
					decision.publish.length > 0,
					`armed with an empty candidate set under order ${order.join('')}`
				);
				armedWithRows += 1;
			}
		}

		// POSITIVE CONTROL. "Never armed on an empty view" is also satisfied by
		// never arming at all, which is the opposite defect: AutoPlay silently
		// dead. Every order containing both an E and an H must reach activation,
		// and 50 of the 81 orders do, so anything less means the sweep stopped
		// exercising the path it claims to guard.
		assert.equal(armedWithRows, 50, 'the sweep must actually reach activation');
	});

	it('progressive playlist hydrate does not freeze AutoPlay on first page', () => {
		const page1 = [row('a', '8A', 120), row('b', '8A', 121), row('c', '8A', 122)];
		const full = [
			...page1,
			row('d', '8A', 123),
			row('e', '8A', 124),
			row('f', '8A', 125)
		];
		const h = makeHarness([], 'playlist-big');
		h.setEnabled(true);
		h.setMembershipHydrating(true);
		const first = h.sort(page1);
		assert.equal(first.snapshotted, false, 'first page must not snapshot while membership hydrates');
		assert.equal(h.snapshot.active, false);
		assert.deepEqual(first.publish.map((r) => r.stable_id), ['a', 'b', 'c']);

		const still = h.sort(page1);
		assert.equal(still.snapshotted, false);
		assert.equal(h.snapshot.active, false);

		h.setMembershipHydrating(false);
		const settled = h.sort(full);
		assert.equal(settled.snapshotted, true, 'settled membership must snapshot the full view');
		assert.equal(h.snapshot.active, true);
		assert.deepEqual(
			settled.publish.map((r) => r.stable_id),
			['a', 'b', 'c', 'd', 'e', 'f']
		);
		assert.equal(h.sort([...full].reverse()).publish, null, 'sort after snapshot stays frozen');
	});

	it('control: a running snapshot survives a same-playlist refill', () => {
		// Overshoot guard: the hydrate gate must only delay ACTIVATION. A
		// background refill of the playlist already playing must not drop the
		// running set back to the live view.
		const rows = [row('a', '8A', 120), row('b', '8A', 121), row('c', '8A', 122)];
		const h = makeHarness(rows, 'playlist-big');
		h.setEnabled(true);
		assert.equal(h.snapshot.active, true);
		h.setMembershipHydrating(true);
		assert.equal(h.sort([...rows].reverse()).publish, null, 'running set stays frozen');
		assert.equal(h.snapshot.active, true);
	});
});

/**
 * The suite above proves the snapshot MODULE. It would keep passing if
 * BrowserPanel were reverted to publishing pane.rows straight through, which
 * is the original bug. This guard reads the wiring itself, because that
 * regression is invisible to every other test here.
 */
describe('BrowserPanel feeds AutoPlay through the snapshot', () => {
	const SOURCE = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)),
		'utf8'
	);

	it('publishes only what the snapshot allows, and only once', () => {
		const calls = SOURCE.split('setAutoPlayTrackFeed(').length - 1;
		assert.equal(calls, 1, 'a second feed publisher would race the snapshot');
		assert.ok(
			SOURCE.includes(
				'if (decision.publish !== null) setAutoPlayTrackFeed(pane.playlist_id, decision.publish);'
			),
			'the feed must publish only a non-null snapshot decision'
		);
	});

	it('snapshots the sorted view, not raw pane membership', () => {
		assert.match(
			SOURCE,
			/autoPlayFeed\.step\(\s*uiPrefs\.auto_play_enabled,\s*pane\.playlist_id,\s*renderedRows\.map\(/,
			'AutoPlay must be fed the active playlist scope and renderedRows (sorted + filtered, including explicit search recovery)'
		);
	});

	it('passes an explicit filter identity, separate from the frozen sort order', () => {
		assert.match(
			SOURCE,
			/autoPlayFeed\.step\([\s\S]*autoPlayFilterKey/,
			'AutoPlay must receive filter changes so it can replace a stale candidate set'
		);
	});

	it('gates snapshot activation on progressive playlist membership settle', () => {
		assert.match(
			SOURCE,
			/pane\.load_progress !== null/,
			'AutoPlay must not snapshot while fillPlaylistPane still reports load_progress'
		);
	});

	it('plainly identifies when the visible order differs from the running snapshot', () => {
		assert.match(
			SOURCE,
			/autoPlaySnapshotMatchesView\s*=\s*autoPlayFeed\.matches\(visibleRows\)/,
			'the mismatch state must compare the frozen snapshot with the current visible rows'
		);
		assert.match(
			SOURCE,
			/autoPlaySnapshotActive\s*=\s*autoPlayFeed\.active/,
			'the component must copy the plain snapshot getter into reactive Svelte state'
		);
		assert.match(
			SOURCE,
			/uiPrefs\.auto_play_enabled && autoPlaySnapshotActive && !autoPlaySnapshotMatchesView/,
			'the notice must render from reactive component state'
		);
		assert.match(
			SOURCE,
			/AutoPlay is using its activation order\. Toggle it off and on to use this order\./,
			'the browser must tell the user which order is live and how to adopt the visible order'
		);
	});

	it('reads the chart rank only while the AutoPlay sort is selected', () => {
		assert.match(
			SOURCE,
			/const autoPlayRankOf = pane\.sort_key === 'autoplay' \? getAutoPlayRankOf\(\) : undefined/,
			'ordinary column sorts must not subscribe the whole table to AutoPlay polling'
		);
	});

	// Review finding (P2, non-blocking, PR #1121): the first cut of this effect
	// only cleared panes[activePane]. Sort pane A by AutoPlay, switch to pane B,
	// disable AutoPlay - pane A never re-mounts, so its stale sort_key survives
	// and resurfaces (sorted by a frozen rank map) the moment the user tabs back.
	it('clears AutoPlay sorting from every pane on disable, not only the active one', () => {
		assert.match(
			SOURCE,
			/for \(const p of panes\)\s*\{\s*if \(p\.sort_key === 'autoplay'\) p\.toggleSort\('autoplay'\);\s*\}/,
			'the disable effect must sweep every pane, since only one pane is ever mounted'
		);
		assert.doesNotMatch(
			SOURCE,
			/panes\[activePane\]\.sort_key === 'autoplay'/,
			'the effect must not go back to reading only the active pane'
		);
	});
});
