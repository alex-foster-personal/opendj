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
 * [if] a compatible candidate already failed to LOAD for this source [then] the
 *   stall blames the files, not key and tempo [⛔️ if it tells the operator to
 *   widen a pitch range when the pitch range was never the problem], and NAMES
 *   the candidates that failed [⛔️ if it names the surviving incompatible rows
 *   instead, which is where `remaining` points once the failures are
 *   quarantined out of it].
 * [if] the operator opens a different playlist after a load failure [then] the
 *   next dead end does NOT claim the new playlist's candidates failed to load
 *   [⛔️ if attempt state outlives the feed it was measured on].
 * [if] AutoPlay is switched off while a handoff is still awaiting [then] the
 *   late rejection raises nothing [⛔️ if a stop banner sits over a
 *   switched-off feature, and rides module state into the next mount].
 * [if] AutoPlay is toggled off and on again before an old handoff rejects
 *   [then] that rejection still raises nothing [⛔️ if a dead handoff restores
 *   its stale stall into the REPLACEMENT session, which "is AutoPlay on right
 *   now" cannot tell apart].
 * [if] a refused MASTER handover is recorded [then] it is retired only when the
 *   REFUSED deck is itself the audible master [⛔️ if any audible master clears
 *   it: a rejected setDeckMaster leaves the OUTGOING deck master and playing,
 *   so the very next poll would erase a banner whose fault is still true].
 * [if] the stall is raised [then] the condition reaches `/api/v1/client-errors`
 *   through the REAL reporter, not a stubbed one [⛔️ if the only server-side
 *   trace of a stopped set is a POST nobody checked was sent].
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { installTimerProbe, loadRuneModule } from './load-rune-module.mjs';

/** Long enough for several real 250 ms polls plus effect flush. */
const POLL_SETTLE_MS = 900;

const ENTRY = [
	"export { installAutoPlay } from '$lib/rb/auto-play.svelte';",
	"export { setAutoPlayEnabled, setAutoPlayEnforceOrder, uiPrefs } from '$lib/rb/prefs.svelte';",
	"export { setAutoPlayTrackFeed } from '$lib/rb/auto-play';",
	"export { deckStates } from '$lib/rb/audio-engine.svelte';",
	"export { readAutoPlayStall, noteAutoPlayHandoffStall } from '$lib/rb/autoplay-stall.svelte';"
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

test('a candidate that failed to LOAD blames the files, not key and tempo', async () => {
	await withController(async (mod) => {
		// One compatible-but-unloadable candidate, then nothing else playable.
		// It is quarantined in _unplayableIds and drops out of `remaining`, so
		// the next poll dead-ends with a remainder that looks merely
		// incompatible. It is not: the files would not open.
		mod.setAutoPlayTrackFeed('playlist-a', [
			{ stable_id: 'src-1', key: '8A', bpm: 124, file_exists: true, title: 'Source', artist: 'Ann' },
			{ stable_id: 'ghost-1', key: '8A', bpm: 124, file_exists: true, title: 'Ghost', artist: 'Bo' },
			{ stable_id: 'far-1', key: '3B', bpm: 175, file_exists: true, title: 'Far', artist: 'Cy' }
		]);
		armSourceDeck(mod.deckStates, { positionMs: 95_000 });
		// The load dispatch rejects (no engine behind the harness), which is the
		// retryable phase, so the candidate is quarantined and AutoPlay re-arms.
		await settle();
		await settle();

		const stall = mod.readAutoPlayStall();
		assert.notEqual(stall, null, 'precondition: AutoPlay reached a terminal branch');
		assert.equal(
			stall.reason,
			'candidates-failed-to-load',
			`a load failure must not be reported as a key/BPM dead end (got ${stall.reason})`
		);
		assert.match(stall.resume, /key and tempo are not the problem/);
		// BOTH were attempted here: with Enforce play order off, the picker's
		// fallback takes any playable unplayed row, so `far-1` was tried too and
		// also failed. That makes the point harder, not softer - `remaining` is
		// EMPTY once both are quarantined, so the previous code named nothing at
		// all and the operator was told files failed without being told which.
		assert.deepEqual(
			stall.blocked.map((t) => t.stable_id).sort(),
			['far-1', 'ghost-1'],
			'the named files must be the ones that FAILED; `remaining` has excluded them'
		);
		assert.equal(stall.blocked_total, 2);
	});
});

test('a new playlist does not inherit the last one\'s load failures', async () => {
	await withController(async (mod) => {
		mod.setAutoPlayTrackFeed('playlist-a', [
			{ stable_id: 'src-1', key: '8A', bpm: 124, file_exists: true, title: 'Source', artist: 'Ann' },
			{ stable_id: 'ghost-1', key: '8A', bpm: 124, file_exists: true, title: 'Ghost', artist: 'Bo' }
		]);
		armSourceDeck(mod.deckStates, { positionMs: 95_000 });
		await settle();
		await settle();
		assert.equal(
			mod.readAutoPlayStall()?.reason,
			'candidates-failed-to-load',
			'precondition: playlist A really did record a load failure'
		);

		// A different playlist, same source deck still master. Enforce play
		// order with the source LAST, so the picker dead-ends without attempting
		// anything at all: nothing in playlist B has failed to load, and saying
		// it did would be the app reporting a failure that never happened.
		mod.setAutoPlayEnforceOrder(true);
		mod.setAutoPlayTrackFeed('playlist-b', [
			{ stable_id: 'b-1', key: '3B', bpm: 175, file_exists: true, title: 'Bee', artist: 'Di' },
			{ stable_id: 'src-1', key: '8A', bpm: 124, file_exists: true, title: 'Source', artist: 'Ann' }
		]);
		await settle();
		await settle();

		assert.equal(
			mod.readAutoPlayStall()?.reason,
			'no-next-in-order',
			'attempt state that outlives its feed makes the app claim failures that never happened'
		);
	});
});

test('a late handoff rejection raises nothing once AutoPlay is off', async () => {
	await withController(async (mod) => {
		mod.setAutoPlayEnabled(false);
		await settle();
		// Exactly what the awaited catch block does when it resumes after the
		// operator switched AutoPlay off: the controller passes its own live
		// state, and a torn-down controller reports false.
		mod.noteAutoPlayHandoffStall('handoff-incomplete', 'src-1', 'play refused', false);
		assert.equal(
			mod.readAutoPlayStall(),
			null,
			'a stop banner over a switched-off feature also rides module state into the next mount'
		);

		// CONTROL: the same call from a live controller still records.
		mod.noteAutoPlayHandoffStall('handoff-incomplete', 'src-1', 'play refused', true);
		assert.notEqual(
			mod.readAutoPlayStall(),
			null,
			'a guard that refuses everything would pass the assertion above and hide every handoff stall'
		);
	});
});

/**
 * A DECLARED BLIND SPOT, and the reason this one guard is source-shaped.
 *
 * Mutating `_armedAt` to ignore the generation left every behavioural test in
 * this file GREEN. That is a finding, not a relief (.claude/rules/verification
 * .md): the driven tests reach the stall module's `stillArmed` guard, which
 * they mutation-prove, but they cannot reach the CONTROLLER's generation
 * comparison, because doing so needs a handoff dispatch that is still pending
 * across a disarm and a re-arm, and `dispatchPerformanceCommand` is in-process
 * with no seam a test can hold open.
 *
 * So the comparison is pinned by shape until such a seam exists, and this test
 * is labelled rather than counted as behavioural coverage. It reds on the
 * mutation that the driven tests could not see.
 */
/**
 * DECLARED BLIND SPOT, same shape as the arming-generation one below.
 *
 * `_promoteMaster`'s failure path cannot be driven from this harness: it fires
 * only after `_handoff` reaches its commit point, which needs a real
 * AudioContext and a real engine behind `dispatchPerformanceCommand`. What IS
 * driven here is everything downstream of the raise - the descriptor, the
 * store, and the reason-specific retire above - so what this guard adds is
 * that the controller's catch actually calls it.
 */
test('SHAPE GUARD: a refused master promotion records a durable stall', () => {
	const source = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/auto-play.svelte.ts', import.meta.url)),
		'utf8'
	);
	const at = source.indexOf("type: 'master', deck: pending.deck");
	assert.notEqual(at, -1, 'the master dispatch could not be located: this guard asserts nothing');
	const block = source.slice(at, source.indexOf('} finally {', at));
	assert.match(block, /pushToast\(/, 'precondition: the toast this replaces is still there');
	assert.match(
		block,
		/noteAutoPlayHandoffStall\(\s*'master-handover-refused'/,
		'a refused handover leaves the follower playing and unmastered, so nothing ever queues again'
	);
	assert.match(block, /if \(!_armedAt\(generation\)\) return;/, 'and not from a dead arming');
});

test('SHAPE GUARD: nothing in the handoff catch runs for a dead arming', () => {
	const source = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/auto-play.svelte.ts', import.meta.url)),
		'utf8'
	);
	const start = source.indexOf('} catch (error: unknown) {', source.indexOf('await _handoff('));
	assert.notEqual(start, -1, 'the handoff catch could not be located: this guard asserts nothing');
	const body = source.slice(start, source.indexOf('} finally {', start));
	// The guard has to come before ANY reporting or state change, because
	// `pushToast` writes the perf ring and posts to /api/v1/client-errors, and
	// the quarantine below it steers the session that replaced this one.
	const guardAt = body.indexOf('if (!_armedAt(generation)) return;');
	assert.notEqual(guardAt, -1, 'the catch has no arming guard at all');
	for (const effect of ['pushToast(', '_unplayableIds.add(', '_attemptsFor', '_triggeredFor =']) {
		const at = body.indexOf(effect);
		assert.notEqual(at, -1, `${effect} is not in the catch: this guard would assert nothing`);
		assert.ok(guardAt < at, `${effect} runs before the arming guard`);
	}
});

test('SHAPE GUARD: the late-failure check compares the captured arming generation', () => {
	const source = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/auto-play.svelte.ts', import.meta.url)),
		'utf8'
	);
	assert.ok(source.length > 0, 'the controller is empty: this guard would assert nothing');
	assert.match(
		source,
		/generation === _generation/,
		'without this, "is AutoPlay on right now" answers yes for the REPLACEMENT session'
	);
	assert.match(source, /const generation = _generation;/, 'captured BEFORE the await, or it is not a generation');
	// The counter has to MOVE, or the comparison above is always true. Both
	// edges: the arm effect (off/on) and uninstall (unmount/remount).
	assert.equal(
		(source.match(/_generation \+= 1;/g) ?? []).length,
		2,
		'expected one bump in the arm effect and one in uninstall'
	);
});

test('a re-armed AutoPlay does not inherit the previous arming late failure', async () => {
	await withController(async (mod) => {
		mod.setAutoPlayTrackFeed('playlist-a', spentFeed());
		armSourceDeck(mod.deckStates, { positionMs: 95_000 });
		await settle();
		assert.notEqual(mod.readAutoPlayStall(), null, 'precondition: arming N recorded a stall');

		// Toggling off and on is a NEW arming. Teardown cleared the stall; a
		// handoff dispatched under the old arming must not put it back, and
		// "is AutoPlay on right now" answers yes for the replacement.
		mod.setAutoPlayEnabled(false);
		await settle();
		assert.equal(mod.readAutoPlayStall(), null, 'precondition: disarm cleared it');
		mod.setAutoPlayEnabled(true);
		await settle();

		mod.noteAutoPlayHandoffStall('handoff-incomplete', 'src-1', 'stale rejection', false);
		assert.equal(
			mod.readAutoPlayStall(),
			null,
			'a dead arming must not restore its stall into the session that replaced it'
		);
	});
});

test('every raise gets its own revision, so two same-shaped stalls are distinguishable', async () => {
	await withController(async (mod) => {
		mod.noteAutoPlayHandoffStall('handoff-incomplete', 'src-1', 'first', true);
		const first = mod.readAutoPlayStall().revision;
		mod.noteAutoPlayHandoffStall('handoff-incomplete', 'src-1', 'second', true);
		const second = mod.readAutoPlayStall().revision;
		assert.notEqual(
			first,
			second,
			'same reason, same source track: only a per-occurrence id tells these apart, ' +
				'and the banner keys its expanded list on it'
		);
		assert.ok(second > first, 'revisions are monotonic, so later is always distinguishable');
	});
});

test('a spent attempt budget names the three files that failed', async () => {
	await withController(async (mod) => {
		// Three playable candidates, all of which fail to load (no engine behind
		// the harness). Each is claimed before its dispatch, so the budget is
		// spent on three DIFFERENT files - and their ids are the only record of
		// which ones, because the toasts that carried them expire.
		mod.setAutoPlayTrackFeed('playlist-a', [
			{ stable_id: 'src-1', key: '8A', bpm: 124, file_exists: true, title: 'Source', artist: 'Ann' },
			{ stable_id: 'f-1', key: '8A', bpm: 124, file_exists: true, title: 'Fail One', artist: 'Bo' },
			{ stable_id: 'f-2', key: '8A', bpm: 125, file_exists: true, title: 'Fail Two', artist: 'Cy' },
			{ stable_id: 'f-3', key: '9A', bpm: 124, file_exists: true, title: 'Fail Three', artist: 'Di' }
		]);
		armSourceDeck(mod.deckStates, { positionMs: 95_000 });
		await settle();
		await settle();
		await settle();

		const stall = mod.readAutoPlayStall();
		assert.notEqual(stall, null, 'precondition: AutoPlay reached a terminal branch');
		assert.ok(
			stall.blocked_total > 0,
			`the banner must name the files that failed, not just say some did (reason ${stall.reason})`
		);
		for (const track of stall.blocked) {
			assert.match(track.stable_id, /^f-/, 'only failed candidates belong in this list');
		}
	});
});

test('a refused master handover is retired once a master is actually audible', async () => {
	await withController(async (mod) => {
		// The follower is PLAYING and is not master. Nothing will queue after
		// it, so the set ends in silence when it finishes - the class this
		// whole change is about, reached through a different branch.
		mod.noteAutoPlayHandoffStall('master-handover-refused', 'next-1', 'refused', true);
		assert.equal(mod.readAutoPlayStall()?.reason, 'master-handover-refused');

		// The REFUSED track, now audible AS master: what was missing was the
		// flag, not the audio, so this is the recovery. This reason inverts the
		// rule every other one uses, which is why it is special-cased at all.
		const one = mod.deckStates[1];
		one.stable_id = 'next-1';
		one.playing = true;
		one.audible = true;
		one.is_master = true;
		one.duration_ms = 100_000;
		one.position_ms = 1_000;
		await settle();

		assert.equal(
			mod.readAutoPlayStall(),
			null,
			'a stall about a missing master flag must not outlive the flag arriving'
		);
	});
});

test('CONTROL: a refused master handover is NOT retired while nothing is audible', async () => {
	await withController(async (mod) => {
		mod.noteAutoPlayHandoffStall('master-handover-refused', 'next-1', 'refused', true);
		const one = mod.deckStates[1];
		one.stable_id = 'next-1';
		one.playing = true;
		// The optimistic flag without the presented one: the room hears nothing.
		one.audible = false;
		one.is_master = true;
		one.duration_ms = 100_000;
		one.position_ms = 1_000;
		await settle();

		assert.notEqual(
			mod.readAutoPlayStall(),
			null,
			'the reason-specific retire must still require real, presented audio'
		);
	});
});

test('CONTROL: a refused master handover survives the OUTGOING master still playing', async () => {
	await withController(async (mod) => {
		mod.noteAutoPlayHandoffStall('master-handover-refused', 'next-1', 'refused', true);
		assert.equal(mod.readAutoPlayStall()?.reason, 'master-handover-refused');

		// A rejected setDeckMaster leaves the OUTGOING deck master and audible,
		// so a rule that retires on "any audible master" erases this banner on
		// the very next poll while the follower is still unmastered and nothing
		// will queue after it.
		const one = mod.deckStates[1];
		one.stable_id = 'outgoing-1';
		one.playing = true;
		one.audible = true;
		one.is_master = true;
		one.duration_ms = 100_000;
		one.position_ms = 1_000;
		await settle();

		assert.notEqual(
			mod.readAutoPlayStall(),
			null,
			'the outgoing master being audible is the FAULT state, not the recovery'
		);
	});
});
