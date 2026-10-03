/**
 * PERFMODE-04 shed job: eager-stem-decode.
 *
 * decodeStemBuffers (stem-graph.ts) has no existing behavioral test coverage
 * in this suite (it needs a real Web Audio decode pipeline, not available in
 * node:test) - the gate this PR adds is the one line `await
 * awaitEagerStemDecodeSlot()`, covered separately below by a source check.
 * The actual gating LOGIC - the part with a real failure mode if broken -
 * lives entirely in this module and is fully exercised here with a fake
 * shed, matching the real BackgroundDemandShed contract from playing-gate.ts
 * (request() either runs the job's registered `run` immediately, or marks it
 * owed for a later drain).
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let shedModule;

before(async () => {
	shedModule = await loadTypeScriptModule('src/lib/rb/stem-decode-shed.ts');
});

/** Mirrors createBackgroundDemandShed's request(id) contract for one job id. */
function makeFakeShed({ deferred }) {
	return {
		request(id) {
			assert.equal(id, 'eager-stem-decode');
			if (!deferred) void shedModule.resumeEagerStemDecodeOwedJob();
			// deferred: do nothing: the caller must wait for an explicit release.
		}
	};
}

test('with no shed armed, the decode slot resolves immediately (default: not gated)', async () => {
	shedModule.setEagerStemDecodeShed(null);
	let resolved = false;
	await shedModule.awaitEagerStemDecodeSlot().then(() => {
		resolved = true;
	});
	assert.equal(resolved, true);
});

test('while the shed defers, the decode slot does not resolve until released', async () => {
	shedModule.setEagerStemDecodeShed(makeFakeShed({ deferred: true }));

	let resolved = false;
	const pending = shedModule.awaitEagerStemDecodeSlot().then(() => {
		resolved = true;
	});

	await new Promise((resolve) => setTimeout(resolve, 30));
	assert.equal(resolved, false, 'the work effect (decode starting) must be absent while the gate is closed');

	await shedModule.resumeEagerStemDecodeOwedJob();
	await pending;
	assert.equal(resolved, true, 'the work effect must be present once the gate reopens');

	shedModule.setEagerStemDecodeShed(null);
});

test('while not deferred, request() releases the slot right away', async () => {
	shedModule.setEagerStemDecodeShed(makeFakeShed({ deferred: false }));

	let resolved = false;
	await shedModule.awaitEagerStemDecodeSlot().then(() => {
		resolved = true;
	});
	assert.equal(resolved, true, 'an idle/not-elevated shed must never delay the decode');

	shedModule.setEagerStemDecodeShed(null);
});

test('two callers waiting on one deferred episode both release together', async () => {
	shedModule.setEagerStemDecodeShed(makeFakeShed({ deferred: true }));

	let firstResolved = false;
	let secondResolved = false;
	const first = shedModule.awaitEagerStemDecodeSlot().then(() => {
		firstResolved = true;
	});
	const second = shedModule.awaitEagerStemDecodeSlot().then(() => {
		secondResolved = true;
	});

	await new Promise((resolve) => setTimeout(resolve, 20));
	assert.equal(firstResolved, false);
	assert.equal(secondResolved, false);

	await shedModule.resumeEagerStemDecodeOwedJob();
	await Promise.all([first, second]);
	assert.equal(firstResolved, true, 'neither caller is dropped');
	assert.equal(secondResolved, true, 'neither caller is dropped');

	shedModule.setEagerStemDecodeShed(null);
});

// ------------------------------------------------- decodeStemBuffers wiring

test('decodeStemBuffers awaits the eager-stem-decode slot before decoding', () => {
	const source = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/stem-graph.ts', import.meta.url)),
		'utf8'
	);
	const body = source.slice(
		source.indexOf('export async function decodeStemBuffers'),
		source.indexOf('export function createDefaultStemControls')
	);
	assert.ok(body.length > 0, 'if decodeStemBuffers cannot be located this guard asserts nothing');
	const gateIndex = body.indexOf('await awaitEagerStemDecodeSlot(');
	const decodeIndex = body.indexOf('decodeStemParts(');
	assert.notEqual(gateIndex, -1, 'decodeStemBuffers must await the shed slot');
	assert.ok(gateIndex < decodeIndex, 'the shed slot must be awaited BEFORE the CPU-heavy decode starts');
});

// ------------------------------------------------- STEM-47: a named, bounded hold
//
// [if] the shed does not defer [then] the start is 'immediate' and the deck is
//   never told it is waiting
// [if] the shed defers [then] onDeferred fires once, before the wait
// [if] the DJ asks for the stems now [then] the held decode starts as 'forced'
// [if] nothing releases it [then] it starts anyway at the bound, 'timed_out'
// [if] nothing is held [then] releaseEagerStemDecodeNow reports 0

test('an undeferred decode starts immediately and never reports a hold', async () => {
	shedModule.setEagerStemDecodeShed(makeFakeShed({ deferred: false }));
	let deferred = 0;
	const start = await shedModule.awaitEagerStemDecodeSlot({ onDeferred: () => (deferred += 1) });
	assert.equal(start, 'immediate');
	assert.equal(deferred, 0, 'a decode that was never held must not show a waiting state');
	shedModule.setEagerStemDecodeShed(null);
});

test('a deferred decode says so once, and the shed release is reported', async () => {
	shedModule.setEagerStemDecodeShed(makeFakeShed({ deferred: true }));
	let deferred = 0;
	const pending = shedModule.awaitEagerStemDecodeSlot({
		onDeferred: () => (deferred += 1),
		setTimer: () => null,
		clearTimer: () => {}
	});
	assert.equal(deferred, 1, 'the hold must be reported synchronously, before any wait');
	await shedModule.resumeEagerStemDecodeOwedJob();
	assert.equal(await pending, 'released');
	shedModule.setEagerStemDecodeShed(null);
});

test('the DJ can start a held decode, and is told how many were waiting', async () => {
	shedModule.setEagerStemDecodeShed(makeFakeShed({ deferred: true }));
	assert.equal(shedModule.releaseEagerStemDecodeNow(), 0, 'nothing is held yet');
	const timer = { setTimer: () => null, clearTimer: () => {} };
	const first = shedModule.awaitEagerStemDecodeSlot(timer);
	const second = shedModule.awaitEagerStemDecodeSlot(timer);
	assert.equal(shedModule.releaseEagerStemDecodeNow(), 2);
	assert.deepEqual(await Promise.all([first, second]), ['forced', 'forced']);
	shedModule.setEagerStemDecodeShed(null);
});

test('LOAD NOW on one deck starts only that deck\'s held decode', async () => {
	shedModule.setEagerStemDecodeShed(makeFakeShed({ deferred: true }));
	const timer = { setTimer: () => null, clearTimer: () => {} };
	let otherStarted = false;
	const mine = shedModule.awaitEagerStemDecodeSlot({ ...timer, deck: 1 });
	const other = shedModule.awaitEagerStemDecodeSlot({ ...timer, deck: 2 });
	void other.then(() => (otherStarted = true));
	assert.equal(shedModule.releaseEagerStemDecodeNow(1), 1);
	assert.equal(await mine, 'forced');
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(otherStarted, false, 'another deck\'s decode started under the same pressure');
	// The other deck's hold still ends the ordinary way.
	await shedModule.resumeEagerStemDecodeOwedJob();
	assert.equal(await other, 'released');
	shedModule.setEagerStemDecodeShed(null);
});

test('LOAD NOW for a deck\'s new track never starts its old track\'s held decode', async () => {
	shedModule.setEagerStemDecodeShed(makeFakeShed({ deferred: true }));
	const timer = { setTimer: () => null, clearTimer: () => {} };
	let token = 1;
	let oldStarted = false;
	const old = shedModule.awaitEagerStemDecodeSlot({ ...timer, deck: 1, stale: () => token !== 1 });
	void old.then(() => (oldStarted = true));
	token = 2;
	const current = shedModule.awaitEagerStemDecodeSlot({ ...timer, deck: 1, stale: () => token !== 2 });
	assert.equal(shedModule.releaseEagerStemDecodeNow(1), 1);
	assert.equal(await current, 'forced');
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(oldStarted, false, 'the old track\'s decode started beside the new one');
	await shedModule.resumeEagerStemDecodeOwedJob();
	assert.equal(await old, 'released');
	shedModule.setEagerStemDecodeShed(null);
});

test('a held decode whose load went stale never starts decoding', async () => {
	const graph = await loadTypeScriptModule('src/lib/rb/stem-graph.ts');
	shedModule.setEagerStemDecodeShed(null);
	let started = false;
	await assert.rejects(
		graph.decodeStemBuffers({}, {}, [], { stale: () => true, onStart: () => (started = true) }),
		/the load it belongs to is gone/
	);
	assert.equal(started, false, 'a stale load was shown decoding');
});

test('a hold nobody releases ends at the bound instead of lasting the whole track', async () => {
	// PERFMODE-18: kernel pressure is real pressure, which takes the longer of
	// the two bounds (the grading itself is tested in
	// perfmode-18-stem-hold-pressure-graded.test.mjs).
	shedModule.setEagerStemDecodeShed(makeFakeShed({ deferred: true }), () => true);
	let armedMs = null;
	let fire = null;
	let cleared = 0;
	const pending = shedModule.awaitEagerStemDecodeSlot({
		setTimer: (run, ms) => {
			armedMs = ms;
			fire = run;
			return 'handle';
		},
		clearTimer: (handle) => {
			assert.equal(handle, 'handle');
			cleared += 1;
		}
	});
	let settled = false;
	void pending.then(() => (settled = true));
	await new Promise((resolve) => setTimeout(resolve, 20));
	assert.equal(settled, false, 'the bound must not fire before its time');
	assert.equal(armedMs, shedModule.EAGER_STEM_DECODE_MAX_DEFER_MS);
	assert.ok(armedMs > 0 && armedMs <= 15_000, 'a bound longer than 15 s is the old dead-button report');
	fire();
	assert.equal(await pending, 'timed_out');
	assert.equal(cleared, 1);
	// A timed-out decode is running, not held: a later release counts nothing,
	// so a retry reads "already loading" instead of claiming a release.
	assert.equal(shedModule.releaseEagerStemDecodeNow(), 0);
	shedModule.setEagerStemDecodeShed(null);
});

test('a timed-out hold leaves other decodes held and countable', async () => {
	// [if] one hold times out while another is still held [then] a release counts and starts only the held one, [else stop]
	shedModule.setEagerStemDecodeShed(makeFakeShed({ deferred: true }));
	let fire = null;
	const timedOut = shedModule.awaitEagerStemDecodeSlot({
		setTimer: (run) => {
			fire = run;
			return 'a';
		},
		clearTimer: () => {}
	});
	const held = shedModule.awaitEagerStemDecodeSlot({ setTimer: () => 'b', clearTimer: () => {} });
	await new Promise((resolve) => setTimeout(resolve, 10));
	fire();
	assert.equal(await timedOut, 'timed_out');
	assert.equal(shedModule.releaseEagerStemDecodeNow(), 1, 'only the decode still held is counted');
	assert.equal(await held, 'forced');
	shedModule.setEagerStemDecodeShed(null);
});
