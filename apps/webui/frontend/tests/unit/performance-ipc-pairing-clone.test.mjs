import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let ipc;

before(async () => {
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
});

// Sun 6 Sep 2026 live client error log, 228 occurrences in one day, and the
// cause of "auto-play stopped after 3 failed handoffs":
//   DataCloneError: Failed to execute 'structuredClone' on 'Window':
//   #<Object> could not be cloned.
//     at queryPerformanceState (.../performance-ipc.svelte.ts:967:77)
//
// _pairingSnapshot is declared `$state(null)` (performance-ipc.svelte.ts).
// Real Svelte 5 wraps any object assigned to a $state variable in a reactive
// Proxy - and a Proxy, however plain the object it wraps, is never
// structured-cloneable (Node's own structuredClone throws the exact same
// "#<Object> could not be cloned" DataCloneError on `structuredClone(new
// Proxy({}, {}))`). Every other queryPerformanceState() field is rebuilt with
// a fresh spread/map before it is returned; pairing_snapshot was the one
// field that fed the live $state value straight into structuredClone().
test('queryPerformanceState clones a reactive-proxied pairing snapshot without a DataCloneError', () => {
	const snapshot = {
		version: 1,
		beat_sync_max: false,
		decks: [
			{
				deck_id: 1,
				stable_id: 'track-1',
				title: 'Track 1',
				position_ms: 1000,
				timestamp: { unit: 'time', value: 1000 },
				eq_adjusts: [{ band: 'low', value: 0.2 }]
			}
		]
	};
	// A bare pass-through Proxy is the minimal stand-in for what Svelte's
	// $state actually hands back for an object value - the wrapping itself,
	// not any trap behaviour, is what defeats structuredClone.
	const reactiveProxiedSnapshot = new Proxy(snapshot, {});

	const uninstall = ipc.installPairingSnapshotForTest(reactiveProxiedSnapshot);
	try {
		let state;
		assert.doesNotThrow(() => {
			state = ipc.queryPerformanceState();
		}, 'queryPerformanceState must not throw DataCloneError on a $state-proxied pairing snapshot');
		assert.deepEqual(state.pairing_snapshot, snapshot);
		// The returned pairing_snapshot must itself be structured-cloneable -
		// i.e. genuinely unwrapped, not just "didn't throw yet".
		assert.doesNotThrow(() => structuredClone(state.pairing_snapshot));
	} finally {
		uninstall();
	}
});
