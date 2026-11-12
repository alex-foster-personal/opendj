// requirement: SET-13
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { bundleRecorderStatusRetryClient } from './mount-svelte-client.mjs';
import { installQualityBadgeDom } from './quality-badge-client-harness.mjs';

let client;
let teardownDom;

before(async () => {
	teardownDom = installQualityBadgeDom();
	client = await bundleRecorderStatusRetryClient();
});

after(() => teardownDom?.());

/** Drain the real HTTP promise chain and Svelte effects without advancing the timer clock. */
async function settle() {
	for (let i = 0; i < 20; i += 1) {
		await Promise.resolve();
		client.flushSync();
	}
}

async function mountRail(t, initiallyRecordable = true) {
	const requests = [];
	client.toastCalls.length = 0;
	t.mock.method(globalThis, 'fetch', async (request) => {
		requests.push([request.method, new URL(request.url).pathname]);
		assert.deepEqual(requests.at(-1), ['GET', '/api/sets/recorder']);
		throw new TypeError('Load failed');
	});
	t.mock.timers.enable({ apis: ['setTimeout'] });
	const target = document.createElement('div');
	document.body.appendChild(target);
	let instance = client.mount(client.RecorderHarness, {
		target,
		props: { initiallyRecordable }
	});
	async function unmount() {
		if (instance === null) return;
		await client.unmount(instance);
		instance = null;
	}
	t.after(async () => {
		await unmount();
		target.remove();
		client.toastCalls.length = 0;
		t.mock.timers.reset();
		t.mock.restoreAll();
	});
	await settle();
	return { requests, instance, unmount };
}

/** All cancellation tests establish the same retry as the positive control. */
async function pendingRetry(t) {
	const rail = await mountRail(t);
	assert.equal(rail.requests.length, 1);
	t.mock.timers.tick(1999);
	await settle();
	assert.equal(rail.requests.length, 1, 'the retry must wait the full 2 seconds');
	assert.deepEqual(client.toastCalls, [], 'one unreachable read must not raise an error toast');
	return rail;
}

// [if] a recordable status read is unreachable [then] it retries at exactly 2 seconds.
test('unreachable REC status schedules a delayed retry while recordable', async (t) => {
	const rail = await pendingRetry(t);
	t.mock.timers.tick(1);
	await settle();
	assert.equal(rail.requests.length, 2, 'positive control: the delayed request really runs');
	assert.deepEqual(client.toastCalls, []);
});

// [if] REC unmounts before its retry delay [then] no delayed status request runs.
test('unmount cancels the pending REC retry', async (t) => {
	const rail = await pendingRetry(t);
	await rail.unmount();
	t.mock.timers.tick(1);
	await settle();
	assert.equal(rail.requests.length, 1);
	for (let i = 0; i < 4; i += 1) {
		t.mock.timers.tick(8000);
		await settle();
		assert.equal(rail.requests.length, 1);
	}
	assert.deepEqual(client.toastCalls, []);
});

// [if] Library mode disables REC before its retry delay [then] no delayed read runs.
test('Library mode cancels the pending REC retry while the rail stays mounted', async (t) => {
	const rail = await pendingRetry(t);
	rail.instance.setRecordable(false);
	await settle();
	t.mock.timers.tick(1);
	await settle();
	assert.equal(rail.requests.length, 1);
	for (let i = 0; i < 4; i += 1) {
		t.mock.timers.tick(8000);
		await settle();
		assert.equal(rail.requests.length, 1, 'neither retry nor ordinary polling may run');
	}
	assert.deepEqual(client.toastCalls, []);
});

// [if] REC mounts in Library mode [then] neither the initial read nor polling runs.
test('an initially non-recordable rail never reads REC status', async (t) => {
	const rail = await mountRail(t, false);
	assert.equal(rail.requests.length, 0);
	t.mock.timers.tick(32000);
	await settle();
	assert.equal(rail.requests.length, 0);
	assert.deepEqual(client.toastCalls, []);
});
