/**
 * PLAY-08 / issue #1640, proven by RUNNING the real controller.
 *
 * The reported failure is not "autoplay stopped" - stopping with nothing
 * playable left is correct. It is that the ONLY signal was a toast which clears
 * itself after TOAST_DEFAULT_MS, so a set that ended at 18:23:39.614Z on
 * Wed 9 Sep 2026 sat silent for 2h29m with a loaded deck and a blank screen.
 *
 * These drive auto-play.svelte.ts with live runes, a real 250 ms poll and real
 * deck state, and assert on the durable state rather than on the toast.
 *
 * [if] autoplay reaches the end of a playlist whose remaining rows are all
 *   missing/stub audio [then] a stall is recorded naming the cause and the
 *   tracks [⛔️ if the only trace is a toast that expires].
 * [if] the stalled source track then ENDS and every deck goes idle [then] the
 *   stall is still there [⛔️ if the explanation vanishes at the exact moment
 *   the room goes quiet - this is the overshoot control for the clear rule].
 * [if] a different track is playing as master [then] the stall is retired
 *   [⛔️ if a stale banner outlives the silence it described].
 * [if] the master is playing far outside the trigger window [then] no stall is
 *   recorded [⛔️ if a healthy set shows a stop banner - the control that a
 *   raise-everything fix would fail].
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { installTimerProbe, loadRuneModule } from './load-rune-module.mjs';

/** Long enough for several real 250 ms polls plus effect flush. */
const POLL_SETTLE_MS = 900;

const ENTRY = [
	"export { installAutoPlay } from '$lib/rb/auto-play.svelte';",
	"export { setAutoPlayEnabled, uiPrefs } from '$lib/rb/prefs.svelte';",
	"export { setAutoPlayTrackFeed } from '$lib/rb/auto-play';",
	"export { deckStates } from '$lib/rb/audio-engine.svelte';",
	"export { readAutoPlayStall } from '$lib/rb/autoplay-stall.svelte';"
].join('\n');

function settle() {
	return new Promise((resolve) => setTimeout(resolve, POLL_SETTLE_MS));
}

/** A playlist whose only playable row is the one already on the source deck. */
function spentFeed() {
	return [
		{ stable_id: 'src-1', key: '8A', bpm: 124, file_exists: true, title: 'Source', artist: 'Ann' },
		{ stable_id: 'gone-1', key: '8A', bpm: 124, file_exists: false, title: 'Gone One', artist: 'Bo' },
		{ stable_id: 'gone-2', key: '9A', bpm: 125, file_exists: false, title: 'Gone Two', artist: 'Cy' }
	];
}

/** Deck 1 is the playing master; 2-4 are empty, so a follower is available. */
function armSourceDeck(deckStates, { positionMs }) {
	const one = deckStates[1];
	one.stable_id = 'src-1';
	one.playing = true;
	one.is_master = true;
	one.duration_ms = 100_000;
	one.position_ms = positionMs;
	one.key = '8A';
	one.bpm = 124;
}

async function withController(run) {
	const probe = installTimerProbe();
	const realFetch = globalThis.fetch;
	// Toast escalation POSTs to /api/v1/client-errors. Nothing here asserts on
	// it, and an unstubbed fetch turns every error toast into an unhandled
	// rejection that fails an unrelated assertion.
	globalThis.fetch = () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
	let uninstall = null;
	try {
		const mod = await loadRuneModule(ENTRY);
		uninstall = mod.installAutoPlay();
		await run(mod);
	} finally {
		if (uninstall !== null) uninstall();
		globalThis.fetch = realFetch;
		probe.restore();
	}
}

test('a spent playlist records a durable stall naming the cause and the tracks', async () => {
	await withController(async (mod) => {
		mod.setAutoPlayTrackFeed('playlist-a', spentFeed());
		armSourceDeck(mod.deckStates, { positionMs: 95_000 });
		await settle();

		const stall = mod.readAutoPlayStall();
		assert.notEqual(stall, null, 'exhaustion with no playable remainder must leave durable state');
		assert.equal(stall.reason, 'missing-audio');
		assert.equal(stall.source_stable_id, 'src-1');
		assert.equal(stall.blocked_total, 2);
		assert.deepEqual(
			stall.blocked.map((t) => t.stable_id).sort(),
			['gone-1', 'gone-2'],
			'the operator must be able to name the tracks that stopped the set'
		);
	});
});

test('OVERSHOOT CONTROL: the stall survives the source track ending and every deck going idle', async () => {
	await withController(async (mod) => {
		mod.setAutoPlayTrackFeed('playlist-a', spentFeed());
		armSourceDeck(mod.deckStates, { positionMs: 95_000 });
		await settle();
		assert.notEqual(mod.readAutoPlayStall(), null, 'precondition: the stall was raised');

		// Natural end: the engine stops the deck and leaves it loaded. AutoPlay's
		// source becomes null and it clears its own arm state on that branch.
		mod.deckStates[1].playing = false;
		mod.deckStates[1].position_ms = 100_000;
		await settle();

		const stall = mod.readAutoPlayStall();
		assert.notEqual(
			stall,
			null,
			'the room is now silent with a loaded deck: this is precisely when the ' +
				'explanation has to still be on screen'
		);
		assert.equal(stall.reason, 'missing-audio');
	});
});

test('sound coming back on a different track retires the stall', async () => {
	await withController(async (mod) => {
		mod.setAutoPlayTrackFeed('playlist-a', spentFeed());
		armSourceDeck(mod.deckStates, { positionMs: 95_000 });
		await settle();
		assert.notEqual(mod.readAutoPlayStall(), null, 'precondition: the stall was raised');

		// The operator loads and plays something else on the master deck.
		mod.deckStates[1].stable_id = 'rescue-1';
		mod.deckStates[1].position_ms = 1_000;
		mod.deckStates[1].playing = true;
		await settle();

		assert.equal(
			mod.readAutoPlayStall(),
			null,
			'a stale stop banner over a playing set is its own mis-report'
		);
	});
});

test('CONTROL: a healthy master far outside the trigger window records no stall', async () => {
	await withController(async (mod) => {
		mod.setAutoPlayTrackFeed('playlist-a', spentFeed());
		armSourceDeck(mod.deckStates, { positionMs: 1_000 });
		await settle();

		assert.equal(
			mod.readAutoPlayStall(),
			null,
			'a fix that raises on every tick would pass the exhaustion test and fail here'
		);
	});
});
