import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let ipc;

before(async () => {
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
});

test('queue scopes isolate deck loads and coordinate only sync-sensitive commands', () => {
	assert.deepEqual(ipc.PERFORMANCE_PRESET_COMMAND_SCOPES, [1, 2, 3, 4, 'sync']);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'load', deck: 1, stable_id: 'a' }), [
		1
	]);
	assert.deepEqual(
		ipc.performanceCommandQueueScopes({ type: 'pitch_range', deck: 1, range: 8 }),
		[1]
	);
	assert.deepEqual(
		ipc.performanceCommandQueueScopes({ type: 'quantize', deck: 2, enabled: false }),
		[2]
	);
	assert.deepEqual(
		ipc.performanceCommandQueueScopes({ type: 'beat_loop', deck: 2, beats: 4 }),
		[2]
	);
	assert.deepEqual(
		ipc.performanceCommandQueueScopes({ type: 'seek', deck: 3, position_ms: 1000 }),
		[3, 'sync']
	);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'master', deck: 4 }), [4, 'sync']);
	assert.equal(ipc.performanceCommandQueueScopes({ type: 'trim', deck: 2, value: 0.7 }), null);
	assert.equal(ipc.performanceCommandQueueScopes({ type: 'crossfader', value: 0.3 }), null);
});

test('continuous mixer controls execute through IPC immediately and round-trip in query state', async () => {
	await ipc.dispatchPerformanceCommand({ type: 'trim', deck: 2, value: 0.7 });
	await ipc.dispatchPerformanceCommand({ type: 'eq', deck: 2, band: 'mid', value: 0.25 });
	await ipc.dispatchPerformanceCommand({ type: 'fader', deck: 2, value: 0.8 });
	await ipc.dispatchPerformanceCommand({ type: 'assign', deck: 2, assign: 'THRU' });
	await ipc.dispatchPerformanceCommand({ type: 'crossfader', value: 0.3 });
	await ipc.dispatchPerformanceCommand({ type: 'master_volume', value: 0.6 });

	const state = ipc.queryPerformanceState();
	assert.equal(state.command_pending, false);
	assert.equal(state.command_queued, 0);
	assert.deepEqual(state.mixer, {
		crossfader: 0.3,
		master: 0.6,
		channels: {
			1: {
				deck_id: 1,
				trim: 0.5,
				eq_high: 0.5,
				eq_mid: 0.5,
				eq_low: 0.5,
				fader: 1,
				assign: 'A'
			},
			2: {
				deck_id: 2,
				trim: 0.7,
				eq_high: 0.5,
				eq_mid: 0.25,
				eq_low: 0.5,
				fader: 0.8,
				assign: 'THRU'
			},
			3: {
				deck_id: 3,
				trim: 0.5,
				eq_high: 0.5,
				eq_mid: 0.5,
				eq_low: 0.5,
				fader: 1,
				assign: 'A'
			},
			4: {
				deck_id: 4,
				trim: 0.5,
				eq_high: 0.5,
				eq_mid: 0.5,
				eq_low: 0.5,
				fader: 1,
				assign: 'B'
			}
		}
	});

	await ipc.dispatchPerformanceCommand({ type: 'trim', deck: 2, value: 0.5 });
	await ipc.dispatchPerformanceCommand({ type: 'eq', deck: 2, band: 'mid', value: 0.5 });
	await ipc.dispatchPerformanceCommand({ type: 'fader', deck: 2, value: 1 });
	await ipc.dispatchPerformanceCommand({ type: 'assign', deck: 2, assign: 'B' });
	await ipc.dispatchPerformanceCommand({ type: 'crossfader', value: 0.5 });
	await ipc.dispatchPerformanceCommand({ type: 'master_volume', value: 1 });
});
