import assert from 'node:assert/strict';
import { afterEach, before, beforeEach, mock, test } from 'node:test';

import { readFrontendSource as readSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let ready;

before(async () => {
	ready = await loadTypeScriptModule('src/lib/rb/stretch-worklet-ready.ts');
});

beforeEach(() => {
	ready.resetStretchWorkletReadyForTests();
});

afterEach(() => {
	mock.timers.reset();
});

function fakeContext(options = {}) {
	const state = options.state ?? 'running';
	const audioWorklet =
		'audioWorklet' in options ? options.audioWorklet : { addModule: async () => {} };
	const resume =
		options.resume ??
		(async function () {
			this.state = 'running';
		});
	const ctx = { state, audioWorklet, resume };
	return ctx;
}

test('running context does not call resume()', async () => {
	let resumeCalls = 0;
	const ctx = fakeContext({
		resume: async function () {
			resumeCalls += 1;
			this.state = 'running';
		}
	});
	await ready.ensureStretchContextRunnable(ctx);
	assert.equal(resumeCalls, 0);
});

test('suspended context awaits resume() and continues when running', async () => {
	let resumeCalls = 0;
	const ctx = fakeContext({
		state: 'suspended',
		resume: async function () {
			resumeCalls += 1;
			this.state = 'running';
		}
	});
	await ready.ensureStretchContextRunnable(ctx);
	assert.equal(resumeCalls, 1);
	assert.equal(ctx.state, 'running');
});

test('interrupted context awaits resume() like suspended', async () => {
	let resumeCalls = 0;
	const ctx = fakeContext({
		state: 'interrupted',
		resume: async function () {
			resumeCalls += 1;
			this.state = 'running';
		}
	});
	await ready.ensureStretchContextRunnable(ctx);
	assert.equal(resumeCalls, 1);
});

test('resume() that leaves the context non-running throws immediately', async () => {
	const ctx = fakeContext({
		state: 'suspended',
		resume: async function () {
			this.state = 'suspended';
		}
	});
	const started = performance.now();
	await assert.rejects(ready.ensureStretchContextRunnable(ctx), (error) => {
		assert.equal(error.name, 'StretchProcessorError');
		assert.match(error.message, /AudioContext is suspended/);
		assert.match(error.message, /ready handshake cannot run/);
		return true;
	});
	assert.ok(performance.now() - started < 1000);
});

test('missing audioWorklet fails synchronously through ensureStretchWorkletReady', async () => {
	const ctx = fakeContext({ audioWorklet: undefined });
	await assert.rejects(ready.ensureStretchWorkletReady(ctx), (error) => {
		assert.equal(error.name, 'StretchProcessorError');
		assert.match(error.message, /AudioWorklet is unavailable/);
		return true;
	});
});

test('addModule is awaited once per context and memoized', async () => {
	let addCalls = 0;
	const ctx = fakeContext({
		audioWorklet: {
			addModule: async () => {
				addCalls += 1;
			}
		}
	});
	await ready.addStretchWorkletModule(ctx, '/stretch.mjs');
	await ready.addStretchWorkletModule(ctx, '/stretch.mjs');
	assert.equal(addCalls, 1);
});

test('addModule rejection names addModule and clears the memo for retry', async () => {
	let addCalls = 0;
	const ctx = fakeContext({
		audioWorklet: {
			addModule: async () => {
				addCalls += 1;
				if (addCalls === 1) throw new Error('network refused');
			}
		}
	});
	await assert.rejects(ready.addStretchWorkletModule(ctx, '/stretch.mjs'), (error) => {
		assert.equal(error.name, 'StretchProcessorError');
		assert.match(error.message, /addModule/);
		assert.match(error.message, /network refused/);
		return true;
	});
	await ready.addStretchWorkletModule(ctx, '/stretch.mjs');
	assert.equal(addCalls, 2);
});

test('addModule hang rejects with addModule timed out, not processor creation', async () => {
	mock.timers.enable({ apis: ['setTimeout'] });
	const ctx = fakeContext({
		audioWorklet: {
			addModule: () => new Promise(() => {})
		}
	});
	const pending = ready.addStretchWorkletModule(ctx, '/stretch.mjs');
	await new Promise((resolve) => queueMicrotask(resolve));
	mock.timers.tick(15_000);
	await assert.rejects(pending, (error) => {
		assert.equal(error.name, 'StretchProcessorError');
		assert.match(error.message, /addModule/);
		assert.match(error.message, /timed out/);
		assert.doesNotMatch(error.message, /processor creation/);
		return true;
	});
});

test('ready module source does not contain processor creation timed out', () => {
	const source = readSource('src/lib/rb/stretch-worklet-ready.ts');
	assert.ok(!source.includes('processor creation timed out'));
});
