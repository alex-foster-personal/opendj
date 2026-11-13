// requirement: PLAY-11, AUTODJ-13764
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/auto-play-save-queue.ts');
});

function harness(answers) {
	const timers = [];
	const toasts = [];
	let calls = 0;
	const queue = mod.createSaveQueue({
		save: async () => {
			const answer = answers[calls++];
			if (answer !== 'ok') throw new Error(answer);
		},
		schedule: (fn, ms) => timers.push({ fn, ms }),
		onGiveUp: (message, retry) => toasts.push({ message, retry })
	});
	const flush = () => new Promise((r) => setImmediate(r));
	const tick = async () => {
		const t = timers.shift();
		t.fn();
		await flush();
		return t.ms;
	};
	return { queue, timers, toasts, tick, flush, calls: () => calls };
}

test('failed save is retried with 1s then 4s backoff and success clears it with no duplicate', async () => {
	/** [if] save fails twice then succeeds [then] retries at 1 s and 4 s, pending clears, no extra save, [else stop]. */
	const h = harness(['played answered 503: x', 'played answered 503: x', 'ok', 'ok']);
	await h.queue.enqueue({});
	assert.equal(h.queue.state().pendingSaves, 1);
	assert.equal(await h.tick(), 1000);
	assert.equal(await h.tick(), 4000);
	assert.equal(h.calls(), 3);
	assert.equal(h.timers.length, 0);
	assert.equal(h.toasts.length, 0);
	assert.equal(h.queue.state().pendingSaves, 0);
});

test('three failed retries end in the error toast with a working Retry and the item stays pending', async () => {
	/** [if] the first try and all three retries fail [then] one toast with Retry, pending stays with last error, [else stop]. */
	const h = harness([
		'played answered 500: a',
		'played answered 500: b',
		'played answered 500: c',
		'played answered 500: d',
		'ok'
	]);
	await h.queue.enqueue({});
	const delays = [await h.tick(), await h.tick(), await h.tick()];
	assert.deepEqual(delays, [1000, 4000, 15000]);
	assert.equal(h.toasts.length, 1);
	assert.equal(h.toasts[0].message, mod.SAVE_FAILED_TOAST);
	assert.deepEqual(h.queue.state(), { pendingSaves: 1, lastError: 'played answered 500: d' });
	h.toasts[0].retry();
	await h.flush();
	assert.equal(h.queue.state().pendingSaves, 0);
});

test('a 4xx invalid request is not retried and toasts the server message', async () => {
	/** [if] the save answers 422 [then] no retry is scheduled and the toast carries the server message, [else stop]. */
	const h = harness(['played answered 422: bad pair']);
	await h.queue.enqueue({});
	assert.equal(h.timers.length, 0);
	assert.equal(h.calls(), 1);
	assert.match(h.toasts[0].message, /bad pair/);
	assert.equal(mod.isInvalidRequestError(new Error('played answered 429: slow')), false);
});
