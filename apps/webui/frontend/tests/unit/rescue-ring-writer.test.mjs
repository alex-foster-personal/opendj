import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// requirement: RESCUE-01

const API_BASE = 'https://engine.example.test';

let originalFetch;

async function _loadWriter() {
	return loadTypeScriptModule('tests/unit/fixtures/rescue-ring-writer-entry.ts', {
		viteApiBase: API_BASE
	});
}

afterEach(() => {
	if (originalFetch !== undefined) globalThis.fetch = originalFetch;
});

test('shouldPostPeriodicRescue gates gig posture, playing, and 2s cadence', async () => {
	const writer = await _loadWriter();
	assert.equal(writer.shouldPostPeriodicRescue(5000, 0, true, 'gig'), true);
	assert.equal(writer.shouldPostPeriodicRescue(1500, 0, true, 'gig'), false);
	assert.equal(writer.shouldPostPeriodicRescue(5000, 0, false, 'gig'), false);
	assert.equal(writer.shouldPostPeriodicRescue(5000, 0, true, 'prep'), false);
});

test('transport commands are registered for rescue hooks', async () => {
	const writer = await _loadWriter();
	const types = writer.rescueTransportCommandTypes();
	assert.ok(types.has('play'));
	assert.ok(types.has('load'));
	assert.ok(types.has('crossfader'));
});

test('installRescueRingWriter arms and disarms hooks', async () => {
	const writer = await _loadWriter();
	writer.installRescueRingWriterHooks();
	writer.uninstallRescueRingWriterHooks();
	const ring = writer.installRescueRingWriter({
		setInterval: () => 1,
		clearInterval: () => {}
	});
	ring.dispose();
});

test('transport event during in-flight POST schedules a follow-up snapshot', async () => {
	const writer = await _loadWriter();
	writer.resetRescueRingWriterForTest();
	writer.uiPrefs.app_posture = 'gig';
	writer.installRescueRingWriterHooks();

	let resolveFirst;
	const firstPending = new Promise((resolve) => {
		resolveFirst = resolve;
	});
	let postCount = 0;
	originalFetch = globalThis.fetch;
	globalThis.fetch = async (url, init) => {
		assert.equal(url, `${API_BASE}/api/v1/performance/rescue-snapshots`);
		assert.equal(init.method, 'POST');
		postCount += 1;
		if (postCount === 1) await firstPending;
		return new Response('{}', { status: 202 });
	};

	writer.notifyRescueTransportEvent({ type: 'play', deck: 1, playing: true });
	await new Promise((resolve) => setTimeout(resolve, writer.RESCUE_TRANSPORT_DEBOUNCE_MS + 20));
	assert.equal(postCount, 1, 'debounced transport should start one POST');

	writer.notifyRescueTransportEvent({ type: 'play', deck: 1, playing: false });
	resolveFirst();
	for (let attempt = 0; attempt < 20 && postCount < 2; attempt += 1) {
		await new Promise((resolve) => setImmediate(resolve));
	}
	assert.equal(postCount, 2, 'in-flight transport must schedule a follow-up POST');

	writer.uninstallRescueRingWriterHooks();
});
