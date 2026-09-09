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
 * [if] a different track is AUDIBLE as master [then] the stall is retired
 *   [⛔️ if a stale banner outlives the silence it described].
 * [if] a different track is `playing` but not `audible` - a play was requested
 *   into a dead output device [then] the stall STAYS [⛔️ if the clear rule
 *   deletes the explanation while the room is still silent, which is the very
 *   class the banner exists for].
 * [if] the master is playing far outside the trigger window [then] no stall is
 *   recorded [⛔️ if a healthy set shows a stop banner - the control that a
 *   raise-everything fix would fail].
 * [if] the stall is raised [then] the condition reaches `/api/v1/client-errors`
 *   through the REAL reporter, not a stubbed one [⛔️ if the only server-side
 *   trace of a stopped set is a POST nobody checked was sent].
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

/**
 * The browser globals `reportClientError` reads before it will POST at all.
 *
 * Without them it returns on `typeof window === 'undefined'` and the escalation
 * test would pass against a path that never ran, which is the failure Sol's
 * round-1 P1 named.
 */
function installBrowserShim() {
	const saved = new Map();
	const set = (key, value) => {
		saved.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
		Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
	};
	set('window', globalThis);
	set('location', { href: 'http://127.0.0.1:0/performance' });
	set('navigator', { userAgent: 'autoplay-stall-persistence-test' });
	set('isSecureContext', true);
	return () => {
		for (const [key, descriptor] of saved) {
			if (descriptor === undefined) delete globalThis[key];
			else Object.defineProperty(globalThis, key, descriptor);
		}
	};
}

async function withController(run) {
	const probe = installTimerProbe();
	const restoreGlobals = installBrowserShim();
	const realFetch = globalThis.fetch;
	/**
	 * Every request the client made, so a test can assert on the real one.
	 *
	 * openapi-fetch hands `fetch` a Request OBJECT, so the method and body live
	 * on it rather than in `init`. Reading only `init` recorded the client-error
	 * POST as a GET with a null body, which is the shape of a recorder that
	 * quietly answers a different question than the one asked.
	 */
	const posts = [];
	globalThis.fetch = async (input, init = {}) => {
		const request = typeof input === 'string' || input instanceof URL ? null : input;
		const rawBody =
			typeof init.body === 'string'
				? init.body
				: request === null
					? null
					: await request.clone().text();
		posts.push({
			url: String(request === null ? input : request.url),
			method: init.method ?? request?.method ?? 'GET',
			body: rawBody === null || rawBody === '' ? null : JSON.parse(rawBody)
		});
		return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
	};
	let uninstall = null;
	try {
		const mod = await loadRuneModule(ENTRY);
		uninstall = mod.installAutoPlay();
		await run(mod, posts);
	} finally {
		if (uninstall !== null) uninstall();
		globalThis.fetch = realFetch;
		restoreGlobals();
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

		// The operator loads and plays something else on the master deck, and the
		// presented-transport observation confirms it is actually coming out.
		mod.deckStates[1].stable_id = 'rescue-1';
		mod.deckStates[1].position_ms = 1_000;
		mod.deckStates[1].playing = true;
		mod.deckStates[1].audible = true;
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

test('the stall condition reaches /api/v1/client-errors through the real reporter', async () => {
	await withController(async (mod, posts) => {
		mod.setAutoPlayTrackFeed('playlist-a', spentFeed());
		armSourceDeck(mod.deckStates, { positionMs: 95_000 });
		await settle();
		assert.notEqual(mod.readAutoPlayStall(), null, 'precondition: the stall was raised');

		const reports = posts.filter(
			(post) => post.method === 'POST' && post.url.endsWith('/api/v1/client-errors')
		);
		assert.equal(
			reports.length,
			1,
			`expected exactly one client-error POST, saw ${posts.map((p) => `${p.method} ${p.url}`).join(', ') || 'no requests at all'}`
		);
		const payload = reports[0].body;
		assert.equal(payload.kind, 'ui-error');
		assert.equal(payload.context.source, 'toast');
		assert.match(
			payload.message,
			/remaining playlist tracks are missing\/stub audio/,
			'the row in webui-client-errors-*.log must name the cause, not just that something failed'
		);
	});
});

test('CONTROL: a healthy set posts no client error', async () => {
	await withController(async (mod, posts) => {
		mod.setAutoPlayTrackFeed('playlist-a', spentFeed());
		armSourceDeck(mod.deckStates, { positionMs: 1_000 });
		await settle();
		assert.deepEqual(
			posts.filter((post) => post.url.endsWith('/api/v1/client-errors')),
			[],
			'a recorder that fires on every run cannot prove the one above'
		);
	});
});

test('OVERSHOOT CONTROL: a play requested into a dead output does not retire the stall', async () => {
	await withController(async (mod) => {
		mod.setAutoPlayTrackFeed('playlist-a', spentFeed());
		armSourceDeck(mod.deckStates, { positionMs: 95_000 });
		await settle();
		assert.notEqual(mod.readAutoPlayStall(), null, 'precondition: the stall was raised');

		// `playing` is written the moment a play is REQUESTED. With the output
		// device dead, `audible` never follows and the room hears nothing.
		mod.deckStates[1].stable_id = 'rescue-1';
		mod.deckStates[1].position_ms = 1_000;
		mod.deckStates[1].playing = true;
		mod.deckStates[1].audible = false;
		await settle();

		assert.notEqual(
			mod.readAutoPlayStall(),
			null,
			'an optimistic play flag is not sound: gating on it would delete the ' +
				'explanation with the room still silent (Codex r3973806301)'
		);
	});
});
