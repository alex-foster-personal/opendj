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
});
