import assert from 'node:assert/strict';
import { afterEach, before, beforeEach, describe, it } from 'node:test';
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
			contents:
				`export default function SvelteComponentStub() { throw new Error(${JSON.stringify(
					`${args.path} is a Svelte component stub under node tests; mounting it needs a browser`
				)}); }`,
			loader: 'js'
		}));
	}
};

async function loadTrackifyControllerEntry() {
	const result = await build({
		entryPoints: [ENTRY],
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: LIB_ROOT, '$lib/rb/performance-ipc.svelte': STUB_IPC },
		bundle: true,
		define: {
			'$state': 'globalThis.__musicDjToolsTestState',
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
	return importBundledSource(result.outputFiles[0].text, 'trackify-autoplay-entry');
}

const row = (stable_id, key = '8A', bpm = 124) => ({
	stable_id,
	key,
	bpm,
	file_exists: true
});

/** @type {Awaited<ReturnType<typeof loadTrackifyControllerEntry>>} */
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

describe(
	'trackify autoplay controller',
	{ concurrency: false },
	() => {
	before(async () => {
		entry = await loadTrackifyControllerEntry();
	});

	function resetControllerState() {
		const uninstall = entry.installTrackifyAutoplay();
		uninstall();
	}

	beforeEach(() => {
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

	it('load failures quarantine and advance within the skip deadline', async () => {
		entry.e2ePrimeTrackifyFeed([row('bad'), row('good')]);
		resetDeck();

		entry.setDispatchPerformanceCommandStub(async (cmd) => {
			if (cmd.type === 'load' && cmd.stable_id === 'bad') {
				return new Promise(() => {});
			}
		});

		const started = performance.now();
		await entry.e2eForceTrackifyLoad('bad');
		const elapsed = performance.now() - started;

		assert.ok(
			elapsed < entry.TRACKIFY_LOAD_SKIP_DEADLINE_MS + 1000,
			`expected load skip within deadline, took ${elapsed.toFixed(0)}ms`
		);

		const loads = entry
			.getDispatchPerformanceCommandCalls()
			.filter((cmd) => cmd.type === 'load');
		assert.equal(loads.length, 2);
		assert.equal(loads[0].stable_id, 'bad');
		assert.equal(loads[1].stable_id, 'good');
	});

	it('skip-next while in flight is re-latched instead of interleaving advances', async () => {
		entry.e2ePrimeTrackifyFeed([row('current'), row('next')]);
		const deck = entry.deckStates[entry.TRACKIFY_DECK_ID];
		deck.stable_id = 'current';
		deck.playing = true;
		deck.position_ms = 199_000;
		deck.duration_ms = 200_000;
		deck.key = '8A';
		deck.bpm = 124;

		let releaseLoad;
		const loadGate = new Promise((resolve) => {
			releaseLoad = resolve;
		});

		entry.setDispatchPerformanceCommandStub(async (cmd) => {
			if (cmd.type === 'load') {
				await loadGate;
			}
		});

		const uninstall = entry.installTrackifyAutoplay();
		// First poll tick (POLL_MS=250) fires the end-of-track advance and hangs on
		// its 'load' dispatch. Wait past that tick before requesting a skip, so the
		// skip lands while an advance is genuinely already in flight - the exact
		// scenario the race requires. Requesting skip before the first tick ever
		// runs would just have that same tick consume it, proving nothing.
		await new Promise((resolve) => setTimeout(resolve, 350));
		const callsBeforeSkip = entry.getDispatchPerformanceCommandCalls();
		const loadCallsBeforeSkip = callsBeforeSkip.filter((cmd) => cmd.type === 'load');
		assert.equal(
			loadCallsBeforeSkip.length,
			1,
			`expected the end-of-track advance to already be in flight before requesting skip (${JSON.stringify(callsBeforeSkip)})`
		);
		entry.requestTrackifySkipNext();
		// Wait past the SECOND poll tick (t=500 from install) while the first
		// advance is still gated. A buggy controller reads and acts on the skip
		// latch unconditionally here, starting a second, concurrent _advance and
		// dispatching a second overlapping 'load'.
		await new Promise((resolve) => setTimeout(resolve, 350));
		const callsWhileGated = entry.getDispatchPerformanceCommandCalls();
		const loadCallsWhileGated = callsWhileGated.filter((cmd) => cmd.type === 'load');
		assert.equal(
			loadCallsWhileGated.length,
			1,
			`skip must not start a second advance while load is in flight (${JSON.stringify(callsWhileGated)})`
		);
		releaseLoad();
		await new Promise((resolve) => setTimeout(resolve, 400));
		uninstall();
	});
	}
);
