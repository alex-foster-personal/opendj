import assert from 'node:assert/strict';
import { rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { importBundledSource } from './import-bundled-source.mjs';
import { bundleTypeScriptModule, loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const runtime = await loadTypeScriptModule('src/lib/rb/library-mode-runtime.ts');

const PROBE_ENTRY_REL = 'tests/unit/.tmp_library_mode_probe_entry.ts';

async function loadRuntimeWithRegistry() {
	const entryAbs = join(FRONTEND_ROOT, PROBE_ENTRY_REL);
	writeFileSync(
		entryAbs,
		`export { readLibraryModeIdleProbe } from '$lib/rb/library-mode-runtime';
export { registerAudioContext, resetAudioContextRegistryForTest } from '$lib/rb/audio-context-registry';
`
	);
	try {
		const text = await bundleTypeScriptModule(PROBE_ENTRY_REL);
		return await importBundledSource(text, PROBE_ENTRY_REL);
	} finally {
		rmSync(entryAbs, { force: true });
	}
}

const STEM_POOL_PROBE_ENTRY_REL = 'tests/unit/.tmp_library_mode_stem_pool_probe_entry.ts';

/** `releaseGigRuntime` and `disposeStemDecoderPools` bundled TOGETHER, so they
 * share the same `flac-decoder-pool.ts` module-level state (`_liveDecoders`,
 * `_pools`) -- `loadTypeScriptModule` esbuild-bundles its whole dependency
 * tree per call, so a separately loaded `flac-decoder-pool.ts` module in
 * another test file is a DIFFERENT instance and could never observe what
 * `releaseGigRuntime` disposes. */
async function loadRuntimeWithStemPool() {
	const entryAbs = join(FRONTEND_ROOT, STEM_POOL_PROBE_ENTRY_REL);
	writeFileSync(
		entryAbs,
		`export * from '$lib/rb/library-mode-runtime';
export { takeDecoder, returnDecoder, activeStemWorkerCount } from '$lib/player/decode/flac-decoder-pool';
`
	);
	try {
		const text = await bundleTypeScriptModule(STEM_POOL_PROBE_ENTRY_REL);
		return await importBundledSource(text, STEM_POOL_PROBE_ENTRY_REL);
	} finally {
		rmSync(entryAbs, { force: true });
	}
}

test('readLibraryModeIdleProbe reports leaked audio contexts when engine state is uninitialized', async () => {
	const mod = await loadRuntimeWithRegistry();
	mod.resetAudioContextRegistryForTest();
	mod.registerAudioContext({});
	const probe = mod.readLibraryModeIdleProbe();
	assert.equal(probe.audio_context_state, 'uninitialized');
	assert.equal(probe.audio_context_count, 1);
	mod.resetAudioContextRegistryForTest();
});

test('readLibraryModeIdleProbe returns serializable counts', () => {
	const probe = runtime.readLibraryModeIdleProbe();
	assert.equal(typeof probe.audio_context_count, 'number');
	assert.equal(typeof probe.stem_decoder_pooled_count, 'number');
	assert.equal(typeof probe.anlz_cache_entry_count, 'number');
	assert.equal(typeof probe.prefetch_ready_count, 'number');
	assert.equal(typeof probe.deck_nodes_present, 'boolean');
	assert.equal(probe.deck_pcm_bytes, 0, 'an engine that never loaded a deck holds no decoded PCM');
});

test('releaseGigRuntime is idempotent', async () => {
	const calls = [];
	runtime.resetLibraryModeRuntimeForTest();
	await runtime.releaseGigRuntime({
		disposeEngine: async () => {
			calls.push('dispose');
		}
	});
	assert.deepEqual(calls, ['dispose']);
	await runtime.releaseGigRuntime({
		disposeEngine: async () => {
			calls.push('dispose-again');
		}
	});
	assert.deepEqual(calls, ['dispose']);
});

test('releaseGigRuntime still propagates a disposal rejection whose reason is undefined', async () => {
	// Sol P1, PR #4034, discussion_r4131311112: the rejection was tracked as
	// `let disposeError: unknown` and rethrown via `disposeError !== undefined`,
	// which cannot tell "disposeEngine() rejected with undefined" apart from
	// "disposeEngine() succeeded" -- a real `Promise.reject()` (no argument)
	// or `throw undefined` silently looked like success, so the release went
	// on to clear the performance session and publish Library idle over a
	// disposal that never finished.
	runtime.resetLibraryModeRuntimeForTest();
	await assert.rejects(
		() =>
			runtime.releaseGigRuntime({
				disposeEngine: async () => {
					throw undefined;
				}
			}),
		(error) => error === undefined,
		'the undefined rejection reason must propagate, not be swallowed as success'
	);
});

test('releaseGigRuntime still disposes the engine and restores mute after a stem-pool cleanup failure', async () => {
	// Codex P2, PR #4034, discussion_r4133146896: `disposeStemDecoderPools()`
	// deliberately propagates its own failure (flac-decoder-pool-dispose.test.mjs
	// covers that contract in isolation), but pre-fix an un-caught rejection here
	// aborted the whole IIFE before `disposeEngine()` or the mute-restoration
	// callbacks ever ran -- leaving the route hard-muted at 0 with a stale
	// pending-master capture forever, on top of whatever the stem failure was.
	const mod = await loadRuntimeWithStemPool();
	mod.resetLibraryModeRuntimeForTest();

	const decoder = {
		ready: Promise.resolve(),
		decodeFile: async () => ({ channelData: [new Float32Array(1)], samplesDecoded: 1, sampleRate: 44100 }),
		reset: async () => {},
		free: async () => {
			throw new Error('stem worker unreachable');
		}
	};
	const taken = await mod.takeDecoder(() => decoder, 'test-release-gig-runtime');
	mod.returnDecoder(taken, 'test-release-gig-runtime');
	assert.equal(mod.activeStemWorkerCount(), 1);

	let disposed = false;
	let onDisposedCalled = false;
	await assert.rejects(
		() =>
			mod.releaseGigRuntime({
				disposeEngine: async () => {
					disposed = true;
				},
				onDisposed: () => {
					onDisposedCalled = true;
				}
			}),
		/stem worker unreachable/,
		'the stem-pool failure must still propagate, not be swallowed'
	);
	assert.equal(disposed, true, 'disposeEngine() must still run despite the earlier stem-pool failure');
	assert.equal(
		onDisposedCalled,
		true,
		'the mute-restoration callback must still run despite the earlier stem-pool failure'
	);
	// A decoder whose free() rejected stays counted as live (the same contract
	// flac-decoder-pool-dispose.test.mjs asserts directly).
	assert.equal(mod.activeStemWorkerCount(), 1);

	mod.resetLibraryModeRuntimeForTest();
});

test('releaseGigRuntime aborts a stale release rather than disposing a remounted Gig engine', async () => {
	runtime.resetLibraryModeRuntimeForTest();
	globalThis.window = {};

	const disposeCalls = [];

	// Start a release for generation 0. Its disposeEngine remounts Gig
	// (bumping to generation 1) WHILE it is running -- simulating browser
	// Back or a quick mode flip landing back on Gig mid-teardown, before the
	// stale release has finished disposing.
	const stalePromise = runtime.releaseGigRuntime({
		disposeEngine: async () => {
			disposeCalls.push('dispose-stale');
			runtime.noteGigRuntimeMounted();
		}
	});
	await stalePromise;

	assert.deepEqual(disposeCalls, ['dispose-stale']);
	// The generation-0 release DID call disposeEngine (it had already passed
	// the pre-disposeEngine fence), but must not publish idle or mark itself
	// released once it notices the remount afterward -- that would report a
	// clean teardown over a live, remounted Gig.
	assert.notEqual(globalThis.window.__mdtLibraryModeIdle, true);
	assert.equal(globalThis.window.musicDjToolsLibraryMode, undefined);

	delete globalThis.window;
	runtime.resetLibraryModeRuntimeForTest();
});

test('releaseGigRuntime does not let a newer generation join an older in-flight release', async () => {
	runtime.resetLibraryModeRuntimeForTest();
	globalThis.window = {};

	const disposeCalls = [];

	// Start a release for generation 0, then remount before its first await
	// (disposeStemDecoderPools) resolves -- the fence catches this at the
	// earliest checkpoint, so this stale release's own disposeEngine is
	// never invoked at all.
	const stalePromise = runtime.releaseGigRuntime({
		disposeEngine: async () => {
			disposeCalls.push('dispose-stale');
		}
	});
	runtime.noteGigRuntimeMounted();

	// The critical assertion: a release requested for the NEW generation
	// while the stale one is still in flight must run its OWN teardown, not
	// silently resolve by joining the stale in-flight `_releasePromise` --
	// pre-fix, this call would have returned the stale promise unchanged and
	// 'dispose-fresh' would never have been pushed at all.
	const freshPromise = runtime.releaseGigRuntime({
		disposeEngine: async () => {
			disposeCalls.push('dispose-fresh');
		}
	});
	await freshPromise;
	assert.deepEqual(disposeCalls, ['dispose-fresh']);
	assert.equal(globalThis.window.__mdtLibraryModeIdle, true);

	await stalePromise;
	assert.deepEqual(disposeCalls, ['dispose-fresh']);

	delete globalThis.window;
	runtime.resetLibraryModeRuntimeForTest();
});

test('shouldSkipPerformanceSessionRestore follows library exit marker', () => {
	runtime.resetLibraryModeRuntimeForTest();
	const store = new Map();
	globalThis.sessionStorage = {
		getItem: (key) => store.get(key) ?? null,
		setItem: (key, value) => {
			store.set(key, value);
		},
		removeItem: (key) => {
			store.delete(key);
		}
	};
	assert.equal(runtime.shouldSkipPerformanceSessionRestore(), false);
	runtime.markLibraryModeExit();
	assert.equal(runtime.shouldSkipPerformanceSessionRestore(), true);
	runtime.consumeLibraryModeExitFlag();
	assert.equal(runtime.shouldSkipPerformanceSessionRestore(), false);
	delete globalThis.sessionStorage;
});

test('clearPerformanceSessionForLibraryExit removes session only when flagged', () => {
	runtime.resetLibraryModeRuntimeForTest();
	const sessionStore = new Map();
	const sessionStorageStore = new Map();
	globalThis.sessionStorage = {
		getItem: (key) => sessionStorageStore.get(key) ?? null,
		setItem: (key, value) => {
			sessionStorageStore.set(key, value);
		},
		removeItem: (key) => {
			sessionStorageStore.delete(key);
		}
	};
	const storage = {
		getItem: (key) => sessionStore.get(key) ?? null,
		setItem: (key, value) => {
			sessionStore.set(key, value);
		},
		removeItem: (key) => {
			sessionStore.delete(key);
		}
	};
	storage.setItem('mdt.rb.performance-session.v1', '{"version":1}');
	runtime.clearPerformanceSessionForLibraryExit(storage);
	assert.equal(storage.getItem('mdt.rb.performance-session.v1'), '{"version":1}');
	runtime.markLibraryModeExit();
	runtime.clearPerformanceSessionForLibraryExit(storage);
	assert.equal(storage.getItem('mdt.rb.performance-session.v1'), null);
	delete globalThis.sessionStorage;
});
