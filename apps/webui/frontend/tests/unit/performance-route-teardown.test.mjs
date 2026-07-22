import assert from 'node:assert/strict';
import test from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const teardown = await loadTypeScriptModule('src/lib/rb/performance-route-teardown.ts');

function _deferred() {
	let resolve;
	let reject;
	const promise = new Promise((promiseResolve, promiseReject) => {
		resolve = promiseResolve;
		reject = promiseReject;
	});
	return { promise, resolve, reject };
}

test('route teardown hard-mutes and starts disposal without awaiting a stalled graceful stop', async () => {
	const graceful = _deferred();
	const disposal = _deferred();
	const calls = [];
	const begin = teardown.createFailClosedPerformanceTeardown({
		hardMute() {
			calls.push('mute');
		},
		gracefulStop() {
			calls.push('stop');
			return graceful.promise;
		},
		dispose() {
			calls.push('dispose');
			return disposal.promise;
		},
		reportError(phase) {
			calls.push(`error:${phase}`);
		}
	});

	const complete = begin();
	assert.deepEqual(calls, ['mute', 'stop', 'dispose']);
	disposal.resolve();
	await complete;
	assert.deepEqual(calls, ['mute', 'stop', 'dispose']);
	graceful.resolve();
});

test('route teardown surfaces a rejected graceful stop without delaying disposal', async () => {
	const gracefulError = new Error('transport stop failed');
	const reported = [];
	let disposeCount = 0;
	const begin = teardown.createFailClosedPerformanceTeardown({
		hardMute() {},
		gracefulStop: () => Promise.reject(gracefulError),
		dispose() {
			disposeCount += 1;
			return Promise.resolve();
		},
		reportError: (phase, error) => reported.push({ phase, error })
	});

	await begin();
	await Promise.resolve();
	assert.equal(disposeCount, 1);
	assert.deepEqual(reported, [{ phase: 'graceful-stop', error: gracefulError }]);
});

test('route teardown still disposes after hard-mute failure and reports disposal failure', async () => {
	const muteError = new Error('mute failed');
	const disposeError = new Error('close failed');
	const reported = [];
	let disposeCount = 0;
	const begin = teardown.createFailClosedPerformanceTeardown({
		hardMute() {
			throw muteError;
		},
		gracefulStop: () => Promise.resolve(),
		dispose() {
			disposeCount += 1;
			return Promise.reject(disposeError);
		},
		reportError: (phase, error) => reported.push({ phase, error })
	});

	await begin();
	assert.equal(disposeCount, 1);
	assert.deepEqual(reported, [
		{ phase: 'hard-mute', error: muteError },
		{ phase: 'dispose', error: disposeError }
	]);
});

test('route teardown is idempotent and never disposes twice', async () => {
	const disposal = _deferred();
	let muteCount = 0;
	let stopCount = 0;
	let disposeCount = 0;
	const begin = teardown.createFailClosedPerformanceTeardown({
		hardMute() {
			muteCount += 1;
		},
		gracefulStop() {
			stopCount += 1;
			return Promise.resolve();
		},
		dispose() {
			disposeCount += 1;
			return disposal.promise;
		},
		reportError() {}
	});

	const first = begin();
	const second = begin();
	assert.equal(first, second);
	assert.deepEqual([muteCount, stopCount, disposeCount], [1, 1, 1]);
	disposal.resolve();
	await first;
	await begin();
	assert.deepEqual([muteCount, stopCount, disposeCount], [1, 1, 1]);
});
