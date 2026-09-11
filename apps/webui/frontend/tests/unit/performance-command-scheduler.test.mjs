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

test('gesture work claims an idle scope synchronously and rejects a busy scope without queuing', async () => {
	const scheduler = new ScopedCommandScheduler();
	const gate = deferred();
	const regular = scheduler.run(['headphone'], async () => gate.promise);
	let invoked = false;
	assert.throws(
		() => scheduler.runImmediatelyIfIdle('headphone', () => {
			invoked = true;
			return Promise.resolve();
		}),
		/headphone.*busy/i
	);
	assert.equal(invoked, false);
	gate.resolve();
	await regular;

	const acquired = scheduler.runImmediatelyIfIdle('headphone', () => {
		invoked = true;
		return Promise.resolve('acquired');
	});
	assert.equal(invoked, true);
	assert.equal(await acquired, 'acquired');
});

function withTimeout(promise, ms, message) {
	let timer;
	const timeout = new Promise((_resolve, reject) => {
		timer = setTimeout(() => reject(new Error(message)), ms);
	});
	return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

test('a detached run() fired from inside another run(), never awaited by it, does not deadlock even when a successor claims both scopes', async () => {
	// r3913492572 / r3913693383: an earlier design nested a SECOND `run`
	// (claiming other decks + 'sync') inside the first and AWAITED it before
	// the outer claim's own promise resolved - that is what created the
	// cycle: a successor B claiming [1, 'sync'], submitted while the outer
	// claim (A, on [1]) was still queued, captures A's tail as ITS
	// predecessor; A's own tail then (because A awaited the inner claim)
	// transitively depended on the inner claim, which - once B occupies
	// 'sync' first - depends on B. B -> A -> inner -> B is the cycle. The
	// fix is not a new primitive: it is that A must never await or return
	// the inner claim's promise. Fired and left detached, A's tail resolves
	// on its own, breaking the only edge that closed the cycle, and the
	// detached claim (correctly) still serializes behind whichever of B / it
	// reaches 'sync' first.
	const scheduler = new ScopedCommandScheduler();
	const predecessorGate = deferred();
	const started = [];

	const predecessor = scheduler.run([1], async () => {
		started.push('predecessor');
		await predecessorGate.promise;
	});

	let detached;
	const outer = scheduler.run([1], async () => {
		started.push('outer-start');
		// Fire the wide claim WITHOUT awaiting or returning it - this is the
		// fix. Do not turn this into `return scheduler.run(...)`.
		detached = scheduler.run([2, 3, 'sync'], async () => {
			started.push('detached-wide-work');
		});
		started.push('outer-end');
	});

	// Submitted AFTER `outer` claims [1] but BEFORE `outer`'s body runs the
	// detached claim - this must queue behind `outer`, never become
	// something the detached claim (or outer) is stuck waiting in a cycle
	// with.
	const successor = scheduler.run([1, 'sync'], async () => {
		started.push('successor');
	});

	await new Promise((resolve) => setImmediate(resolve));
	assert.deepEqual(started, ['predecessor'], 'outer must not start before its scope is free');

	predecessorGate.resolve();
	await withTimeout(
		Promise.all([outer, successor]).then(() => detached),
		2000,
		'a detached wide claim + a later shared-scope successor deadlocked - the r3913492572 cycle reopened'
	);

	assert.deepEqual(
		started,
		['predecessor', 'outer-start', 'outer-end', 'successor', 'detached-wide-work'],
		'outer must finish (and its shared-scope successor must run) without ever waiting on the detached wide claim'
	);
});

test('a detached wide claim that also claims the narrow claim\'s own scope keeps that scope occupied until the wide work finishes', async () => {
	// r3914267990 P1 BLOCKING, found on top of the fix above: excluding the
	// narrow scope (1) from the detached wide claim's own scope list means
	// scope 1 is released the instant the narrow claim's body returns - a
	// fresh command on scope 1, submitted any time after, would run
	// concurrently with the still in-flight detached wide work instead of
	// queuing behind it. Including scope 1 in the wide claim's scopes makes
	// the wide claim the tail map's new occupant of it (predecessors are
	// computed from the CURRENT tail map before the wide claim's own
	// registration overwrites it, so this is an ordinary acyclic "wait for
	// whoever currently holds scope 1" dependency, not a self-await - safe
	// specifically because the narrow claim never awaits the wide claim back).
	const scheduler = new ScopedCommandScheduler();
	const started = [];
	const wideGate = deferred();
	let detached;

	const narrow = scheduler.run([1], async () => {
		started.push('narrow-start');
		detached = scheduler.run([1, 2, 'sync'], async () => {
			started.push('wide-start');
			await wideGate.promise;
			started.push('wide-end');
		});
		started.push('narrow-end');
	});

	await narrow;
	assert.deepEqual(
		started,
		['narrow-start', 'narrow-end'],
		'the narrow claim must resolve without waiting on the detached wide claim'
	);

	// Submitted only AFTER the narrow claim has already resolved. If scope 1
	// were released instead of handed off to the still-running wide claim,
	// this would start immediately and race the wide work on the same scope.
	let lateRan = false;
	const late = scheduler.run([1], async () => {
		lateRan = true;
	});

	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(
		lateRan,
		false,
		'a fresh claim on the narrow scope must not run while the detached wide claim (which also holds that scope) is still in flight - the r3914267990 gap reopened'
	);

	wideGate.resolve();
	await withTimeout(
		Promise.all([detached, late]),
		2000,
		'the late claim on the shared scope never ran - the wide claim deadlocked or never actually released the scope'
	);
	assert.deepEqual(started, ['narrow-start', 'narrow-end', 'wide-start', 'wide-end']);
	assert.equal(lateRan, true);
});

test('session invalidation rejects queued load and play before execution without blocking new work', async () => {
	const scheduler = new ScopedCommandScheduler();
	const predecessorGate = deferred();
	const invoked = [];
	const predecessor = scheduler.run([1], async () => {
		invoked.push('predecessor');
		await predecessorGate.promise;
	});
	const queuedLoad = scheduler.run([1], async () => invoked.push('stale-load'));
	const queuedPlay = scheduler.run([1, 'sync'], async () => invoked.push('stale-play'));

	await new Promise((resolve) => setImmediate(resolve));
	assert.deepEqual(invoked, ['predecessor']);
	scheduler.invalidateQueued('performance route session ended');
	const newSessionCommand = scheduler.run([1], async () => invoked.push('new-session-load'));
	await newSessionCommand;
	assert.deepEqual(invoked, ['predecessor', 'new-session-load']);

	const loadRejected = assert.rejects(queuedLoad, /performance route session ended/i);
	const playRejected = assert.rejects(queuedPlay, /performance route session ended/i);
	predecessorGate.resolve();
	await Promise.all([predecessor, loadRejected, playRejected]);
	assert.deepEqual(invoked, ['predecessor', 'new-session-load']);
});
