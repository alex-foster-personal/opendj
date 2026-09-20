import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const runtime = await loadTypeScriptModule('src/lib/rb/library-mode-runtime.ts');

test('readLibraryModeIdleProbe returns serializable counts', () => {
	const probe = runtime.readLibraryModeIdleProbe();
	assert.equal(typeof probe.audio_context_count, 'number');
	assert.equal(typeof probe.stem_decoder_pooled_count, 'number');
	assert.equal(typeof probe.anlz_cache_entry_count, 'number');
	assert.equal(typeof probe.prefetch_ready_count, 'number');
	assert.equal(typeof probe.deck_nodes_present, 'boolean');
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
