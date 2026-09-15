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
	const gateIndex = body.indexOf('awaitEagerStemDecodeSlot()');
	const decodeIndex = body.indexOf('decodeStemParts(');
	assert.notEqual(gateIndex, -1, 'decodeStemBuffers must await the shed slot');
	assert.ok(gateIndex < decodeIndex, 'the shed slot must be awaited BEFORE the CPU-heavy decode starts');
});
