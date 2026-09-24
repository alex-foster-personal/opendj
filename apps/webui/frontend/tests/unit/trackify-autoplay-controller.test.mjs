import assert from 'node:assert/strict';
import { afterEach, before, beforeEach, describe, it, mock } from 'node:test';
import { existsSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { build } from 'esbuild';

import { importBundledSource } from './import-bundled-source.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const LIB_ROOT = fileURLToPath(new URL('../../src/lib', import.meta.url));
const ENTRY = fileURLToPath(new URL('./fixtures/trackify-autoplay-entry.ts', import.meta.url));

const urlSuffixImports = {
	name: 'vite-url-suffix',
	setup(build) {
		build.onResolve({ filter: /\?url$/ }, (args) => ({
			path: args.path,
			namespace: 'vite-url-suffix'
		}));
		build.onLoad({ filter: /.*/, namespace: 'vite-url-suffix' }, (args) => ({
			contents: `export default ${JSON.stringify(args.path.replace(/\?url$/, ''))};`,
			loader: 'js'
		}));
	}
};

const svelteComponentStubs = {
	name: 'svelte-component-stub',
	setup(build) {
		build.onResolve({ filter: /\.svelte$/ }, (args) => {
			const base = args.path.startsWith('$lib/')
				? join(LIB_ROOT, args.path.slice('$lib/'.length))
				: resolve(args.resolveDir, args.path);
			if (existsSync(`${base}.ts`) || !existsSync(base)) return undefined;
			return { path: args.path, namespace: 'svelte-component-stub' };
		});
		build.onLoad({ filter: /.*/, namespace: 'svelte-component-stub' }, (args) => ({
			contents: `export default function SvelteComponentStub() { throw new Error(${JSON.stringify(
				`${args.path} is a Svelte component stub under node tests; mounting it needs a browser`
			)}); }`,
			loader: 'js'
		}));
	}
};

async function bundleTrackifyControllerEntry() {
	const result = await build({
		entryPoints: [ENTRY],
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: LIB_ROOT },
		bundle: true,
		define: {
			$state: 'globalThis.__musicDjToolsTestState',
			'import.meta.env.DEV': 'true',
			'import.meta.env.VITE_API_BASE': 'undefined'
		},
		format: 'esm',
		logLevel: 'silent',
		platform: 'node',
		plugins: [urlSuffixImports, svelteComponentStubs],
		target: 'node20',
		write: false
	});
	globalThis.__musicDjToolsTestState = (value) => value;
	globalThis.__musicDjToolsTestState.snapshot = (value) =>
		value === undefined ? undefined : JSON.parse(JSON.stringify(value));
	return result.outputFiles[0].text;
}

/** Drains the microtask queue; setImmediate is never in the mocked timer APIs. */
const settle = () => new Promise((resolve) => setImmediate(resolve));

/**
 * Captured before any test enables `mock.timers`, so it always refers to the
 * REAL, unmocked setTimeout -- used only as a wall-clock guard against a
 * mutation-test genuinely wedging this test file forever (see the hard-
 * ceiling test below), never to drive the scenario itself.
 */
const realSetTimeout = setTimeout;

const row = (stable_id, key = '8A', bpm = 124) => ({
	stable_id,
	key,
	bpm,
	file_exists: true
});

/** @type {string} */
let bundleText;
/** @type {Record<string, any>} */
let entry;
/** @type {(() => void) | null} */
let uninstallIpc = null;

const DURATION_MS = 200_000;

/**
 * The audio transport boundary, faked on the engine instance the REAL
 * dispatcher calls. It publishes what the engine publishes (stable_id,
 * duration, position, playing) and nothing else; every command still goes
 * through parsing, the per-deck scope queue and the command session.
 *
 * `loadGates` maps a stable_id to 'reject' or to a promise the load waits on
 * before it publishes, so a test controls exactly when (and whether) a load
 * lands. `log` is the order the ENGINE saw commands in, which is what the
 * scope queue decides, not the order the controller submitted them.
 */
function installFakeTransport() {
	const deck = entry.deckStates[entry.TRACKIFY_DECK_ID];
	const log = [];
	const loadGates = new Map();
	entry.engine.load = async (deckId, stableId) => {
		log.push(`load ${stableId}`);
		const gate = loadGates.get(stableId);
		if (gate === 'reject') throw new Error('decode failed');
		if (gate !== undefined) await gate;
		const target = entry.deckStates[deckId];
		target.stable_id = stableId;
		target.duration_ms = DURATION_MS;
		target.position_ms = 0;
		target.playing = false;
	};
	entry.engine.unload = async (deckId) => {
		const target = entry.deckStates[deckId];
		log.push(`unload ${target.stable_id}`);
		target.stable_id = null;
		target.duration_ms = null;
		target.position_ms = 0;
		target.playing = false;
	};
	entry.engine.play = async (deckId) => {
		log.push(`play ${entry.deckStates[deckId].stable_id}`);
		entry.deckStates[deckId].playing = true;
	};
	entry.engine.pause = async (deckId) => {
		log.push(`pause ${entry.deckStates[deckId].stable_id}`);
		entry.deckStates[deckId].playing = false;
	};
	return { deck, log, loadGates };
}

function resetDeck() {
	const deck = entry.deckStates[entry.TRACKIFY_DECK_ID];
	deck.stable_id = null;
	deck.playing = false;
	deck.position_ms = 0;
	deck.duration_ms = null;
	deck.key = null;
	deck.bpm = null;
}

function gate() {
	let release = () => {};
	const promise = new Promise((resolve) => {
		release = resolve;
	});
	return { promise, release };
}

/** Park the deck as the engine would publish a loaded track. */
function loadedDeck(deck, stableId, { playing, position_ms }) {
	deck.stable_id = stableId;
	deck.duration_ms = DURATION_MS;
	deck.position_ms = position_ms;
	deck.playing = playing;
	deck.key = '8A';
	deck.bpm = 124;
}

describe('trackify autoplay controller (real performance dispatcher)', { concurrency: false }, () => {
	before(async () => {
		bundleText = await bundleTrackifyControllerEntry();
	});

	function resetControllerState() {
		const uninstall = entry.installTrackifyAutoplay();
		uninstall();
	}

	beforeEach(async () => {
		// A fresh module evaluation per test: the PLAY-04 feed snapshot, the
		// skip latch, the controller's sets and the dispatcher's command
		// session are module state, and the feed snapshot deliberately ignores
		// re-priming once taken, so sharing one instance would run later tests
		// against an earlier test's feed.
		entry = await importBundledSource(bundleText, 'trackify-autoplay-entry');
		resetControllerState();
		globalThis.window = {};
		uninstallIpc = entry.installPerformanceBrowserIpc();
		entry.uiPrefs.auto_play_enabled = true;
		resetDeck();
		entry.e2ePrimeTrackifyFeed([]);
	});

	afterEach(() => {
		resetControllerState();
		if (uninstallIpc !== null) uninstallIpc();
		uninstallIpc = null;
		delete globalThis.window;
		entry.uiPrefs.auto_play_enabled = false;
		resetDeck();
		entry.e2ePrimeTrackifyFeed([]);
	});

	// PERFMODE-15 unsupervised criterion: "a failed load skips within 2 s". This
	// bound comes from the requirement, NOT from TRACKIFY_LOAD_SKIP_DEADLINE_MS,
	// so raising the constant past the requirement turns these tests red.
	const REQUIRED_SKIP_BOUND_MS = 2000;

	it('a rejected load is quarantined and the next candidate loads and plays with no clock advance', async () => {
		mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
		try {
			const { log, loadGates } = installFakeTransport();
			entry.e2ePrimeTrackifyFeed([row('bad'), row('good')]);
			loadGates.set('bad', 'reject');
			const toastsBefore = entry.toasts.length;

			const done = entry.e2eForceTrackifyLoad('bad');
			await settle();
			// No simulated time has passed: a load that REJECTS must not wait for
			// the deadline timer at all.
			assert.deepEqual(log, ['load bad', 'load good', 'play good']);
			await done;
			const state = entry.readTrackifyAutoplayState();
			assert.equal(state.queue_head, 'good');
			assert.equal(state.deck.stable_id, 'good');
			assert.equal(state.deck.playing, true);
			assert.match(state.last_skip_reason ?? '', /^skipped bad: decode failed$/);
			const pushed = entry.toasts.slice(toastsBefore).map((toast) => toast.message);
			assert.ok(
				pushed.some((message) => message.includes('Trackify: skipped track (decode failed)')),
				`expected a skip toast, got ${JSON.stringify(pushed)}`
			);
		} finally {
			mock.timers.reset();
		}
	});

	it('a hung load is skipped within 2 s, and its late completion never plays or keeps the deck', async () => {
		mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
		const hung = gate();
		try {
			const { deck, log, loadGates } = installFakeTransport();
			entry.e2ePrimeTrackifyFeed([row('bad'), row('good')]);
			loadGates.set('bad', hung.promise);
			const toastsBefore = entry.toasts.length;

			const done = entry.e2eForceTrackifyLoad('bad');
			await settle();
			assert.deepEqual(log, ['load bad']);

			// Control for the overshoot direction: a deadline that fires early (or
			// no wait at all) would skip a load that is merely slow.
			const justBeforeDeadline =
				Math.min(entry.TRACKIFY_LOAD_SKIP_DEADLINE_MS, REQUIRED_SKIP_BOUND_MS) - 1;
			mock.timers.tick(justBeforeDeadline);
			await settle();
			assert.equal(
				entry.readTrackifyAutoplayState().last_skip_reason,
				null,
				'the skip must not fire before the load deadline elapses'
			);

			// The requirement: by 2 s after the load started, the track is skipped
			// (quarantined, reported, toasted).
			mock.timers.tick(REQUIRED_SKIP_BOUND_MS - justBeforeDeadline);
			await settle();
			assert.match(
				entry.readTrackifyAutoplayState().last_skip_reason ?? '',
				/^skipped bad: .*did not settle within/,
				`a hung load must be skipped within ${REQUIRED_SKIP_BOUND_MS}ms`
			);
			assert.ok(
				entry.toasts
					.slice(toastsBefore)
					.some((toast) => toast.message.startsWith('Trackify: skipped track (')),
				'the skip must be toasted'
			);

			// The dispatcher cannot cancel a running load, and it runs deck 1's
			// commands in order, so the timed-out load still holds the deck. The
			// retry must not start its own deadline behind it: 3 s more of a
			// stale load must not quarantine the healthy next candidate.
			mock.timers.tick(3000);
			await settle();
			assert.deepEqual(log, ['load bad'], 'nothing may reach the engine past the held deck');

			// The timed-out load lands late. The engine publishes it; the
			// superseded sequence must not play it, must take it back off the
			// deck, and only then may the next candidate load and play.
			hung.release();
			await settle();
			await done;
			assert.deepEqual(log, ['load bad', 'unload bad', 'load good', 'play good']);
			const state = entry.readTrackifyAutoplayState();
			assert.equal(state.queue_head, 'good');
			assert.equal(deck.stable_id, 'good');
			assert.equal(deck.playing, true);
			assert.match(state.last_skip_reason ?? '', /^skipped bad: /, 'good must not be quarantined');
		} finally {
			hung.release();
			mock.timers.reset();
		}
	});

	it('a timed-out load with no other candidate is taken back off the deck when it lands late', async () => {
		mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
		const hung = gate();
		try {
			const { deck, log, loadGates } = installFakeTransport();
			entry.e2ePrimeTrackifyFeed([row('bad')]);
			loadGates.set('bad', hung.promise);

			const done = entry.e2eForceTrackifyLoad('bad');
			await settle();
			mock.timers.tick(REQUIRED_SKIP_BOUND_MS);
			await settle();
			await done;
			assert.match(entry.readTrackifyAutoplayState().last_skip_reason ?? '', /^skipped bad: /);

			// Nothing newer will replace it, so the superseded sequence itself
			// must retire the quarantined track the engine publishes late.
			hung.release();
			await settle();
			assert.deepEqual(log, ['load bad', 'unload bad']);
			assert.equal(deck.stable_id, null);
			assert.equal(deck.playing, false);
			assert.equal(entry.readTrackifyAutoplayState().queue_head, null);
		} finally {
			hung.release();
			mock.timers.reset();
		}
	});

	it('a timed-out load does not retire its track once a different session (Gig) has since claimed the shared deck (Sol review round 4)', async () => {
		mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
		const hung = gate();
		try {
			const { deck, log, loadGates } = installFakeTransport();
			entry.e2ePrimeTrackifyFeed([row('bad')]);
			loadGates.set('bad', hung.promise);

			const done = entry.e2eForceTrackifyLoad('bad');
			await settle();
			mock.timers.tick(REQUIRED_SKIP_BOUND_MS);
			await settle();
			await done;
			assert.match(entry.readTrackifyAutoplayState().last_skip_reason ?? '', /^skipped bad: /);

			// Gig mounts and claims the shared deck id while bad's own
			// dispatcher call is still pending -- e.g. the operator navigated
			// Trackify -> Gig while this superseded load was still unwinding.
			// This is the real, production ownership signal (also used by
			// `releaseGigRuntime`/`installTrackifySession`'s own generation
			// checks), independent of this file's own `_installEpoch`, which
			// bumps on Trackify's OWN teardown too -- a case the PRECEDING
			// test (no other session involved) requires to still retire
			// normally.
			entry.noteGigRuntimeMounted();

			// bad's own dispatcher call finally lands late. Its stale
			// retirement must not unload: this deck id no longer belongs to
			// the session that quarantined 'bad'.
			hung.release();
			await settle();
			assert.deepEqual(
				log,
				['load bad'],
				'a stale retirement must not dispatch an unload once a different session has claimed the deck'
			);
		} finally {
			hung.release();
			mock.timers.reset();
		}
	});

	it('advances only once the playing track has reached its end, never inside the old 16 s window', async () => {
		mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
		let uninstall = null;
		try {
			const { deck, log } = installFakeTransport();
			entry.e2ePrimeTrackifyFeed([row('current'), row('next')]);
			loadedDeck(deck, 'current', { playing: true, position_ms: DURATION_MS - 16_000 });

			uninstall = entry.installTrackifyAutoplay();
			for (const remaining of [16_000, 5_000, 1_000, 250]) {
				deck.position_ms = DURATION_MS - remaining;
				mock.timers.tick(250);
				await settle();
				assert.deepEqual(log, [], `${remaining} ms of audible audio left: nothing may unload it`);
			}

			// Control for the overshoot direction: a controller that never
			// advances passes every assertion above.
			deck.position_ms = DURATION_MS;
			mock.timers.tick(250);
			await settle();
			assert.deepEqual(log, ['unload current', 'load next', 'play next']);
			assert.equal(entry.readTrackifyAutoplayState().queue_head, 'next');
		} finally {
			if (uninstall !== null) uninstall();
			mock.timers.reset();
		}
	});

	it('a deck paused before its end never advances; one stopped at its end (natural end) does', async () => {
		mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
		let uninstall = null;
		try {
			const { deck, log } = installFakeTransport();
			entry.e2ePrimeTrackifyFeed([row('current'), row('next')]);
			loadedDeck(deck, 'current', { playing: false, position_ms: DURATION_MS - 1_000 });

			uninstall = entry.installTrackifyAutoplay();
			for (let i = 0; i < 8; i += 1) {
				mock.timers.tick(250);
				await settle();
			}
			assert.deepEqual(log, [], 'Pause near the end must keep playback stopped');

			// Control for the overshoot direction: "never advance a stopped deck"
			// would strand every track the engine's natural-end stop parks at
			// its decoded end.
			deck.position_ms = DURATION_MS;
			mock.timers.tick(250);
			await settle();
			assert.deepEqual(log, ['unload current', 'load next', 'play next']);
		} finally {
			if (uninstall !== null) uninstall();
			mock.timers.reset();
		}
	});

	it('skip-next while in flight is re-latched instead of interleaving advances', async () => {
		mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
		let uninstall = null;
		const firstLoad = gate();
		try {
			const { deck, log, loadGates } = installFakeTransport();
			entry.e2ePrimeTrackifyFeed([row('current'), row('next'), row('third')]);
			loadedDeck(deck, 'current', { playing: false, position_ms: DURATION_MS });
			loadGates.set('next', firstLoad.promise);

			uninstall = entry.installTrackifyAutoplay();
			// First poll tick: the end-of-track advance starts and hangs on 'load'.
			mock.timers.tick(250);
			await settle();
			assert.deepEqual(
				log,
				['unload current', 'load next'],
				'the end-of-track advance must already be in flight before the skip'
			);

			entry.requestTrackifySkipNext();
			// Second poll tick while the first advance is still gated: acting on
			// the skip here would submit a second, overlapping unload/load.
			mock.timers.tick(250);
			await settle();
			assert.equal(
				entry.performanceCommandStatus.deck_pending[entry.TRACKIFY_DECK_ID],
				1,
				'skip must not submit a second advance while a load is in flight'
			);

			// Control for the overshoot direction: the skip must be deferred, not
			// dropped. Once the first advance lands, the next tick acts on it.
			firstLoad.release();
			await settle();
			mock.timers.tick(250);
			await settle();
			assert.deepEqual(
				log,
				['unload current', 'load next', 'play next', 'unload next', 'load third', 'play third'],
				'the deferred skip must run once the in-flight advance finishes'
			);
			assert.equal(entry.readTrackifyAutoplayState().queue_head, 'third');
		} finally {
			firstLoad.release();
			if (uninstall !== null) uninstall();
			mock.timers.reset();
		}
	});

	it('a dispatcher command that never settles at all is eventually released by a hard ceiling, not wedged forever', async () => {
		mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
		const foreverHung = gate();
		try {
			const { log, loadGates } = installFakeTransport();
			entry.e2ePrimeTrackifyFeed([row('bad'), row('good')]);
			loadGates.set('bad', foreverHung.promise);

			const done = entry.e2eForceTrackifyLoad('bad');
			await settle();
			mock.timers.tick(REQUIRED_SKIP_BOUND_MS);
			await settle();
			assert.match(
				entry.readTrackifyAutoplayState().last_skip_reason ?? '',
				/^skipped bad: .*did not settle within/,
				'bad must still be skipped by its own load deadline first'
			);

			// Control for the overshoot direction: shortly after bad's own
			// deadline, the retry to 'good' must still be waiting -- it is
			// queued behind bad's OWN dispatcher call, which `foreverHung`
			// deliberately never releases in this test, so nothing but a
			// ceiling timer can move this forward.
			mock.timers.tick(REQUIRED_SKIP_BOUND_MS);
			await settle();
			assert.deepEqual(
				log,
				['load bad'],
				'the retry must not reach the engine while genuinely wedged behind the hung load'
			);

			// The bug this guards against: without a hard ceiling on
			// `_lastSequenceSettled`, ticking simulated time changes nothing
			// here -- the retry is waiting on a raw promise, not a timer --
			// and the guarded race below times out because `done` never
			// resolves.
			mock.timers.tick(60_000);
			await settle();
			// 'good' now begins its own sequence, queues behind the
			// still-hung 'bad' at the real per-deck dispatcher, and times out
			// on its OWN load deadline in turn.
			mock.timers.tick(REQUIRED_SKIP_BOUND_MS);
			await settle();

			const outcome = await Promise.race([
				done.then(() => 'resolved'),
				new Promise((resolve) => {
					const guard = realSetTimeout(() => resolve('STILL_WEDGED'), 500);
					if (typeof guard.unref === 'function') guard.unref();
				})
			]);
			assert.equal(
				outcome,
				'resolved',
				'a hung dispatcher command must not wedge every later Trackify load forever'
			);
			await done;
			assert.match(
				entry.readTrackifyAutoplayState().last_skip_reason ?? '',
				/^skipped good: .*did not settle within/,
				'good must have been queued behind the still-hung bad and timed out in its own turn'
			);
		} finally {
			foreverHung.release();
			mock.timers.reset();
		}
	});

	it('teardown invalidates an in-flight load: it does not toast or retry into the dead session, and its late completion only retires the track', async () => {
		mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
		const hung = gate();
		let uninstall = null;
		try {
			const { deck, log, loadGates } = installFakeTransport();
			entry.e2ePrimeTrackifyFeed([row('bad'), row('good')]);
			loadGates.set('bad', hung.promise);

			uninstall = entry.installTrackifyAutoplay();
			const toastsBefore = entry.toasts.length;

			const done = entry.e2eForceTrackifyLoad('bad');
			await settle();
			assert.deepEqual(log, ['load bad']);

			// Tear the session down mid-load, before its deadline fires --
			// e.g. the operator navigated away from the Trackify route.
			uninstall();
			uninstall = null;

			mock.timers.tick(REQUIRED_SKIP_BOUND_MS);
			await settle();
			await done;

			assert.deepEqual(
				log,
				['load bad'],
				'a load superseded by teardown must not retry into the dead session'
			);
			assert.equal(
				entry.toasts.length,
				toastsBefore,
				'a load timing out after its own session tore down must not toast into a dead session'
			);
			const state = entry.readTrackifyAutoplayState();
			assert.equal(
				state.last_skip_reason,
				null,
				'a torn-down session must not record a skip reason for a load it no longer owns'
			);
			assert.equal(state.queue_head, null);

			// The late completion, once it lands, must still retire the
			// track: it must not sit occupying a deck id a later session
			// (e.g. Gig, which shares this deck id) may reuse.
			hung.release();
			await settle();
			assert.deepEqual(log, ['load bad', 'unload bad']);
			assert.equal(deck.stable_id, null);
			assert.equal(deck.playing, false);
		} finally {
			if (uninstall !== null) uninstall();
			hung.release();
			mock.timers.reset();
		}
	});

	it('a load queued behind a stale _lastSequenceSettled is superseded, not dispatched, once teardown lands first', async () => {
		mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
		const wedgedBad = gate();
		let uninstall = null;
		try {
			const { log, loadGates } = installFakeTransport();
			// No retry candidate once 'bad' is quarantined: this isolates the
			// SECOND, independent call below from the existing recursive-retry
			// path (which already rechecks installEpoch inside its own catch).
			entry.e2ePrimeTrackifyFeed([row('bad')]);
			loadGates.set('bad', wedgedBad.promise);

			uninstall = entry.installTrackifyAutoplay();

			// First load dispatches 'load bad', then times out on its OWN 2 s
			// deadline -- the underlying dispatcher call (`wedgedBad`) is never
			// released in this test, so it keeps running underneath.
			// `_lastSequenceSettled` keeps tracking that raw, still-pending
			// call up to its 30 s hard ceiling, independently of `_loadAndPlay`
			// having already returned once its own 2 s deadline fired.
			const first = entry.e2eForceTrackifyLoad('bad');
			await settle();
			assert.deepEqual(log, ['load bad']);
			mock.timers.tick(REQUIRED_SKIP_BOUND_MS);
			await settle();
			await first;
			assert.match(
				entry.readTrackifyAutoplayState().last_skip_reason ?? '',
				/^skipped bad: .*did not settle within/
			);

			// A second, independent load starts (e.g. the next poll tick's own
			// advance) while `_lastSequenceSettled` is still the stale, wedged
			// promise from 'bad'. It must block entering its own sequence.
			const second = entry.e2eForceTrackifyLoad('other');
			await settle();
			assert.deepEqual(
				log,
				['load bad'],
				'the second load must queue behind the stale settle, not dispatch yet'
			);

			// Tear the session down while the second load is still waiting on
			// that stale promise -- e.g. the operator navigated away.
			uninstall();
			uninstall = null;

			// The stale sequence's hard ceiling fires, releasing
			// `_lastSequenceSettled` and resuming the second load's
			// `_loadAndPlay` past its `await`.
			mock.timers.tick(30_000);
			await settle();

			// Drain the real per-deck dispatcher queue: 'bad's own engine.load
			// call is what that queue is still serialized behind, so a buggy
			// `_loadAndPlay('other')` that already SUBMITTED a load command
			// (rather than returning before ever calling
			// `_dispatchLoadSequence`) only reaches the engine once this
			// releases -- without releasing it here, 'load other' could never
			// appear in `log` either way, and the assertion below would pass
			// for the wrong reason (control for the "queue, not the fix,
			// blocked it" false pass).
			wedgedBad.release();
			await settle();

			// The bug this guards against: without an install-epoch recheck
			// immediately after `await _lastSequenceSettled`, the resumed call
			// dispatches 'load other' onto a deck id a different session (e.g.
			// Gig, which shares this deck id) may since have claimed.
			await Promise.race([
				second,
				new Promise((resolve) => {
					const guard = realSetTimeout(resolve, 500);
					if (typeof guard.unref === 'function') guard.unref();
				})
			]);
			// The first sequence's own late resolution (its dispatcher call was
			// never cancelled, only timed out at the controller level) still
			// produces its own log entries here -- this test asserts only that
			// the SECOND load ('other') never reaches the engine at all.
			assert.ok(
				!log.some((entry) => entry.includes('other')),
				`a load resumed after teardown must not dispatch into the dead session, got ${JSON.stringify(log)}`
			);
		} finally {
			if (uninstall !== null) uninstall();
			wedgedBad.release();
			mock.timers.reset();
		}
	});

	it('a stale sequence retirement queued behind a newer, already-dispatched load does not unload it (Sol review round 3)', async () => {
		mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
		const badGate = gate();
		const goodGate = gate();
		try {
			const { log, loadGates } = installFakeTransport();
			// 'bad' quarantines into a retry to 'good' via the existing catch
			// block, so 'good' is a genuinely SEPARATE, later-generation
			// sequence -- not a manually-issued second call -- matching the
			// real shape Sol's finding describes.
			entry.e2ePrimeTrackifyFeed([row('bad'), row('good')]);
			loadGates.set('bad', badGate.promise);
			// Gated too, so 'good's own engine.load call reaches the fake
			// transport (claiming the queue tail and logging) but does not
			// publish stable_id='good' until released below -- this pins the
			// window in which bad's own late retirement check can still see
			// `stable_id === 'bad'` while 'good' already holds the tail,
			// which is the exact shape the finding describes: the stable-id
			// check happens at submission time, not at the unload's actual,
			// later execution time.
			loadGates.set('good', goodGate.promise);

			const done = entry.e2eForceTrackifyLoad('bad');
			await settle();
			assert.deepEqual(log, ['load bad']);

			// 'bad' times out on its own 2 s deadline. Its retry ('good')
			// starts, but blocks on `_lastSequenceSettled` -- still 'bad's own
			// raw dispatcher call, which `badGate` deliberately withholds.
			mock.timers.tick(REQUIRED_SKIP_BOUND_MS);
			await settle();
			assert.match(
				entry.readTrackifyAutoplayState().last_skip_reason ?? '',
				/^skipped bad: .*did not settle within/
			);
			assert.deepEqual(
				log,
				['load bad'],
				"the retry to 'good' must not have dispatched yet, still gated on bad's stale settle"
			);

			// The hard ceiling (30 s) releases `_lastSequenceSettled` while
			// bad's OWN dispatcher call is STILL pending -- 'good's load
			// SUBMITS and claims the scope's current tail while bad's own
			// command is still the one it must queue behind.
			mock.timers.tick(30_000);
			await settle();
			assert.deepEqual(
				log,
				['load bad'],
				"'good' must have submitted its load (claiming the queue tail) but not yet reached the engine -- still queued behind bad's own still-pending command"
			);

			// bad's own dispatcher call lands late. 'good's queued load then
			// reaches the fake engine (claiming the tail's own execution slot
			// and logging), and parks on `goodGate` before publishing its
			// stable_id -- deliberately BEFORE bad's own, longer round trip
			// back through the command session/scheduler wrapping resumes its
			// controller code and re-checks `deckStates[deck].stable_id`.
			badGate.release();
			await settle();
			await settle();
			assert.deepEqual(
				log,
				['load bad', 'load good'],
				"'good' must have reached the engine (parked on its own gate) before bad's stale retirement check runs"
			);

			// Release 'good' last: its own load publishes stable_id='good'
			// and its sequence proceeds to play it.
			goodGate.release();
			await settle();
			await settle();

			await Promise.race([
				done,
				new Promise((resolve) => {
					const guard = realSetTimeout(resolve, 500);
					if (typeof guard.unref === 'function') guard.unref();
				})
			]);

			// The bug this guards against: bad's retirement reads
			// `deckStates[deck].stable_id === 'bad'` at SUBMISSION time (true
			// -- 'good' has not yet published its own stable_id) and queues
			// an unconditional unload behind the scope's CURRENT tail, which
			// is by then 'good's own load, not bad's. That unload only
			// executes once 'good' has already loaded (and possibly played),
			// and unloads whatever is on the deck AT THAT POINT: 'good'.
			assert.ok(
				!log.includes('unload good'),
				`a stale sequence's late retirement must never unload a newer, already-dispatched track, got ${JSON.stringify(log)}`
			);
			const state = entry.readTrackifyAutoplayState();
			assert.equal(state.deck.stable_id, 'good');
			assert.equal(state.deck.playing, true);
		} finally {
			badGate.release();
			goodGate.release();
			mock.timers.reset();
		}
	});
});
