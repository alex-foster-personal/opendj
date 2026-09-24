import assert from 'node:assert/strict';
import { afterEach, before, beforeEach, describe, it, mock } from 'node:test';
import { existsSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { build } from 'esbuild';

import { importBundledSource } from './import-bundled-source.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const LIB_ROOT = fileURLToPath(new URL('../../src/lib', import.meta.url));
const STUB_IPC = fileURLToPath(
	new URL('./fixtures/trackify-autoplay-stub-ipc.ts', import.meta.url)
);
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
		alias: { $lib: LIB_ROOT, '$lib/rb/performance-ipc.svelte': STUB_IPC },
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

function resetDeck() {
	const deck = entry.deckStates[entry.TRACKIFY_DECK_ID];
	deck.stable_id = null;
	deck.playing = false;
	deck.position_ms = 0;
	deck.duration_ms = null;
	deck.key = null;
	deck.bpm = null;
}

describe('trackify autoplay controller', { concurrency: false }, () => {
	before(async () => {
		bundleText = await bundleTrackifyControllerEntry();
	});

	function resetControllerState() {
		const uninstall = entry.installTrackifyAutoplay();
		uninstall();
	}

	beforeEach(async () => {
		// A fresh module evaluation per test: the PLAY-04 feed snapshot, the
		// skip latch and the controller's sets are module state, and the feed
		// snapshot deliberately ignores re-priming once taken, so sharing one
		// instance would run later tests against an earlier test's feed.
		entry = await importBundledSource(bundleText, 'trackify-autoplay-entry');
		resetControllerState();
		entry.resetDispatchPerformanceCommandStub();
		entry.setDispatchPerformanceCommandStub(async () => {});
		entry.uiPrefs.auto_play_enabled = true;
		resetDeck();
		entry.e2ePrimeTrackifyFeed([]);
	});

	afterEach(() => {
		resetControllerState();
		entry.resetDispatchPerformanceCommandStub();
		entry.uiPrefs.auto_play_enabled = false;
		resetDeck();
		entry.e2ePrimeTrackifyFeed([]);
	});

	// PERFMODE-15 unsupervised criterion: "a failed load skips within 2 s". This
	// bound comes from the requirement, NOT from TRACKIFY_LOAD_SKIP_DEADLINE_MS,
	// so raising the constant past the requirement turns these tests red.
	const REQUIRED_SKIP_BOUND_MS = 2000;

	function loadCalls() {
		return entry.getDispatchPerformanceCommandCalls().filter((cmd) => cmd.type === 'load');
	}

	it('a rejected load is quarantined and the next candidate loads with no clock advance', async () => {
		mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
		try {
			entry.e2ePrimeTrackifyFeed([row('bad'), row('good')]);
			entry.setDispatchPerformanceCommandStub(async (cmd) => {
				if (cmd.type === 'load' && cmd.stable_id === 'bad') {
					throw new Error('decode failed');
				}
			});
			const toastsBefore = entry.toasts.length;

			const done = entry.e2eForceTrackifyLoad('bad');
			await settle();
			// No simulated time has passed: a load that REJECTS must not wait for
			// the deadline timer at all.
			assert.deepEqual(
				loadCalls().map((cmd) => cmd.stable_id),
				['bad', 'good']
			);
			await done;
			const state = entry.readTrackifyAutoplayState();
			assert.equal(state.queue_head, 'good');
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

	it('a hung load is skipped within 2 s of simulated time, and not before the deadline', async () => {
		mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
		try {
			entry.e2ePrimeTrackifyFeed([row('bad'), row('good')]);
			entry.setDispatchPerformanceCommandStub(async (cmd) => {
				if (cmd.type === 'load' && cmd.stable_id === 'bad') {
					return new Promise(() => {});
				}
			});

			const done = entry.e2eForceTrackifyLoad('bad');
			await settle();
			assert.deepEqual(
				loadCalls().map((cmd) => cmd.stable_id),
				['bad']
			);

			// Control for the overshoot direction: a deadline that fires early (or
			// no wait at all) would skip a load that is merely slow.
			const justBeforeDeadline =
				Math.min(entry.TRACKIFY_LOAD_SKIP_DEADLINE_MS, REQUIRED_SKIP_BOUND_MS) - 1;
			mock.timers.tick(justBeforeDeadline);
			await settle();
			assert.deepEqual(
				loadCalls().map((cmd) => cmd.stable_id),
				['bad'],
				'the skip must not fire before the load deadline elapses'
			);

			// The requirement: by 2 s after the load started, the skip has happened.
			mock.timers.tick(REQUIRED_SKIP_BOUND_MS - justBeforeDeadline);
			await settle();
			assert.deepEqual(
				loadCalls().map((cmd) => cmd.stable_id),
				['bad', 'good'],
				`a hung load must be skipped within ${REQUIRED_SKIP_BOUND_MS}ms`
			);
			await done;
			const state = entry.readTrackifyAutoplayState();
			assert.equal(state.queue_head, 'good');
			assert.match(state.last_skip_reason ?? '', /^skipped bad: .*did not settle within/);
		} finally {
			mock.timers.reset();
		}
	});

	it('skip-next while in flight is re-latched instead of interleaving advances', async () => {
		mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
		let uninstall = null;
		let releaseLoad = () => {};
		try {
			entry.e2ePrimeTrackifyFeed([row('current'), row('next'), row('third')]);
			const deck = entry.deckStates[entry.TRACKIFY_DECK_ID];
			deck.stable_id = 'current';
			deck.playing = false;
			deck.position_ms = 199_000;
			deck.duration_ms = 200_000;
			deck.key = '8A';
			deck.bpm = 124;

			const loadGate = new Promise((resolve) => {
				releaseLoad = resolve;
			});
			let gateFirstLoad = true;
			entry.setDispatchPerformanceCommandStub(async (cmd) => {
				if (cmd.type === 'unload') {
					deck.stable_id = null;
				} else if (cmd.type === 'load') {
					if (gateFirstLoad) {
						gateFirstLoad = false;
						await loadGate;
					}
					deck.stable_id = cmd.stable_id;
					deck.position_ms = 0;
				}
			});

			uninstall = entry.installTrackifyAutoplay();
			// First poll tick: the end-of-track advance starts and hangs on 'load'.
			mock.timers.tick(250);
			await settle();
			assert.deepEqual(
				loadCalls().map((cmd) => cmd.stable_id),
				['next'],
				'the end-of-track advance must already be in flight before the skip'
			);

			entry.requestTrackifySkipNext();
			// Second poll tick while the first advance is still gated: acting on
			// the skip here would start a second, overlapping unload/load.
			mock.timers.tick(250);
			await settle();
			assert.deepEqual(
				loadCalls().map((cmd) => cmd.stable_id),
				['next'],
				'skip must not start a second advance while a load is in flight'
			);

			// Control for the overshoot direction: the skip must be deferred, not
			// dropped. Once the first advance lands, the next tick acts on it.
			releaseLoad();
			await settle();
			mock.timers.tick(250);
			await settle();
			assert.deepEqual(
				loadCalls().map((cmd) => cmd.stable_id),
				['next', 'third'],
				'the deferred skip must run once the in-flight advance finishes'
			);
			assert.equal(entry.readTrackifyAutoplayState().queue_head, 'third');
		} finally {
			releaseLoad();
			if (uninstall !== null) uninstall();
			mock.timers.reset();
		}
	});
});
