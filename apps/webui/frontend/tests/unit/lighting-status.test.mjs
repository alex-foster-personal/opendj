import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/lighting-status.svelte.ts');
});

const res = (status, body) => ({ status, json: async () => body });

test('lighting output disabled by flag shows off', async () => {
	/** [if] lighting output is disabled by feature flag [then] the chip reads "Lights off", [else stop]. */
	assert.equal(await mod.fetchLightingChipState(async () => res(409, null)), 'off');
	assert.equal(mod.describeLightingChip('off').label, 'Lights off');
});

test('connected feed shows on', async () => {
	/** [if] lighting status reports a connected feed [then] the chip reads "Lights on", [else stop]. */
	const body = { enabled: true, feed_connected: true };
	assert.equal(await mod.fetchLightingChipState(async () => res(200, body)), 'on');
	assert.equal(mod.describeLightingChip('on').label, 'Lights on');
});

test('no feed shows no feed', async () => {
	/** [if] lighting status reports no feed [then] the chip reads "Lights: no feed", [else stop]. */
	const body = { enabled: true, feed_connected: false };
	assert.equal(await mod.fetchLightingChipState(async () => res(200, body)), 'no_feed');
	assert.equal(mod.describeLightingChip('no_feed').label, 'Lights: no feed');
	assert.equal(await mod.fetchLightingChipState(async () => res(500, null)), null);
});

/** A fetch that answers flags with `lighting.output` set as given, and records every path. */
function flagsFetch(
	flagsResponse,
	statusResponse = res(200, { enabled: true, feed_connected: true })
) {
	const calls = [];
	const fetchFn = async (path) => {
		calls.push(path);
		if (path === mod.FLAGS_PATH) return flagsResponse;
		if (path === mod.LIGHTING_STATUS_PATH) return statusResponse;
		throw new Error(`unexpected path ${path}`);
	};
	return { calls, fetchFn };
}

const flagsBody = (enabled) => ({ flags: [{ flag_id: 'lighting.output', enabled }] });

test('flag off reads off without calling the status route', async () => {
	/** [if] lighting.output is off in GET /api/v1/flags [then] the chip reads off and the status route is never called, [else stop]. */
	const { calls, fetchFn } = flagsFetch(res(200, flagsBody(false)));
	const first = await mod.pollLightingChip(null, fetchFn);
	assert.deepEqual(first, { outputEnabled: false, state: 'off' });
	assert.deepEqual(await mod.pollLightingChip(first.outputEnabled, fetchFn), first);
	assert.deepEqual(calls, [mod.FLAGS_PATH]);
	assert.equal(mod.lightingOutputEnabled({ flags: [] }), false);
});

test('flag on polls the status route', async () => {
	/** [if] lighting.output is on in GET /api/v1/flags [then] each poll reads the status route and flags are read once, [else stop]. */
	const { calls, fetchFn } = flagsFetch(res(200, flagsBody(true)));
	const first = await mod.pollLightingChip(null, fetchFn);
	assert.deepEqual(first, { outputEnabled: true, state: 'on' });
	await mod.pollLightingChip(first.outputEnabled, fetchFn);
	assert.deepEqual(calls, [mod.FLAGS_PATH, mod.LIGHTING_STATUS_PATH, mod.LIGHTING_STATUS_PATH]);
});

test('flags read failure hides the chip and skips the status route', async () => {
	/** [if] GET /api/v1/flags fails or is malformed [then] the chip state is null and the status route is not called, [else stop]. */
	for (const flagsResponse of [res(500, null), res(200, { flags: 'nope' })]) {
		const { calls, fetchFn } = flagsFetch(flagsResponse);
		assert.deepEqual(await mod.pollLightingChip(null, fetchFn), {
			outputEnabled: null,
			state: null
		});
		assert.deepEqual(calls, [mod.FLAGS_PATH]);
	}
	const thrown = await mod.pollLightingChip(null, async () => {
		throw new Error('offline');
	});
	assert.deepEqual(thrown, { outputEnabled: null, state: null });
});
