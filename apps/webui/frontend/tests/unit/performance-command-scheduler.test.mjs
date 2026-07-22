import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let ScopedCommandScheduler;

before(async () => {
	({ ScopedCommandScheduler } = await loadTypeScriptModule(
		'src/lib/rb/performance-command-scheduler.ts'
	));
});

function deferred() {
	let resolve;
	const promise = new Promise((done) => {
		resolve = done;
	});
	return { promise, resolve };
}

test('different deck scopes run concurrently while one deck remains serialized', async () => {
	const scheduler = new ScopedCommandScheduler();
	const deckOneGate = deferred();
	const started = [];

	const deckOneFirst = scheduler.run([1], async () => {
		started.push('deck-1-first');
		await deckOneGate.promise;
	});
	const deckOneSecond = scheduler.run([1], async () => {
		started.push('deck-1-second');
	});
	const deckTwo = scheduler.run([2], async () => {
		started.push('deck-2');
	});

	await new Promise((resolve) => setImmediate(resolve));
	assert.deepEqual(started, ['deck-1-first', 'deck-2']);
	deckOneGate.resolve();
	await Promise.all([deckOneFirst, deckOneSecond, deckTwo]);
	assert.deepEqual(started, ['deck-1-first', 'deck-2', 'deck-1-second']);
});

test('a multi-deck command forms one ordered barrier only across its declared scopes', async () => {
	const scheduler = new ScopedCommandScheduler();
	const deckOneGate = deferred();
	const barrierGate = deferred();
	const started = [];

	const deckOne = scheduler.run([1], async () => {
		started.push('deck-1');
		await deckOneGate.promise;
	});
	const barrier = scheduler.run([1, 2], async () => {
		started.push('barrier');
		await barrierGate.promise;
	});
	const deckTwo = scheduler.run([2], async () => {
		started.push('deck-2-after');
	});
	const deckThree = scheduler.run([3], async () => {
		started.push('deck-3');
	});

	await new Promise((resolve) => setImmediate(resolve));
	assert.deepEqual(started, ['deck-1', 'deck-3']);
	deckOneGate.resolve();
	await new Promise((resolve) => setImmediate(resolve));
	assert.deepEqual(started, ['deck-1', 'deck-3', 'barrier']);
	barrierGate.resolve();
	await Promise.all([deckOne, barrier, deckTwo, deckThree]);
	assert.deepEqual(started, ['deck-1', 'deck-3', 'barrier', 'deck-2-after']);
});

test('a rejected command does not poison later work on the same deck', async () => {
	const scheduler = new ScopedCommandScheduler();
	const expected = new Error('expected failure');
	const failed = scheduler.run([4], async () => {
		throw expected;
	});
	const recovered = scheduler.run([4], async () => 'recovered');

	await assert.rejects(failed, expected);
	assert.equal(await recovered, 'recovered');
});

test('empty and duplicate scopes fail fast before any command executes', async () => {
	const scheduler = new ScopedCommandScheduler();
	let calls = 0;

	await assert.rejects(scheduler.run([], async () => calls++), /at least one scope/i);
	await assert.rejects(scheduler.run([1, 1], async () => calls++), /duplicate scope/i);
	assert.equal(calls, 0);
});
