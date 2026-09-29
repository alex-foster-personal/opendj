// requirement: PERFMODE-16
// [if] helper on and pressure warning [then] one warn toast per episode, [else stop].
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

test('pressure toast fires once per elevated episode', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/gig-helper-pressure-toast.ts');
	const messages = [];
	mod.resetGigHelperPressureEpisode();
	const deps = {
		pushToast: (message, kind) => {
			assert.equal(kind, 'warn');
			messages.push(message);
		}
	};
	const warning = {
		loadAvg1m: 4,
		memFreeMb: 100,
		swapUsedMb: 0,
		kernelLevel: 2,
		kernelMemoryPressureLevel: 2,
		churnScore: null,
		swapRate: null,
		decompRate: null,
		band: 'warning',
		sampleIntervalMs: 5000,
		compressedMb: null,
		requestedAtMs: 1,
		serverCacheAgeMs: 0
	};
	mod.onGigHelperPressureSnapshot(warning, deps);
	mod.onGigHelperPressureSnapshot(warning, deps);
	assert.equal(messages.length, 1);
	assert.match(messages[0], /warning/);
	mod.onGigHelperPressureSnapshot({ ...warning, band: 'fine' }, deps);
	mod.onGigHelperPressureSnapshot({ ...warning, band: 'critical' }, deps);
	assert.equal(messages.length, 2);
	assert.match(messages[1], /critical/);
});
