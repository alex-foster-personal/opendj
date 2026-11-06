// requirement: TRYER-16
// Issue #9328: TryerLanes.svelte fetch $effect stale-response guard. Split from
// tryer-lanes.test.mjs so TRYER-16 curve/peak tests stay off the GUARD-15 allowlist.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { transformSync } from 'esbuild';

// ---------------------------------------------------------------- TryerLanes.svelte fetch effect (stale response guard)

const TRYER_LANES_SOURCE = readFileSync(
	fileURLToPath(new URL('../../src/lib/tryer/TryerLanes.svelte', import.meta.url)),
	'utf8'
);

function matchBrace(source, openBraceIdx) {
	let depth = 0;
	for (let i = openBraceIdx; i < source.length; i++) {
		if (source[i] === '{') depth++;
		else if (source[i] === '}') {
			depth -= 1;
			if (depth === 0) return i;
		}
	}
	throw new Error('unbalanced braces starting at ' + openBraceIdx);
}

/** Extract `() => { ... }` from `$effect(() => { ... })` whose block contains `anchor`. */
function extractEffectArrow(source, file, anchor) {
	const anchorIdx = source.indexOf(anchor);
	assert.notEqual(anchorIdx, -1, `${anchor} not found in ${file}`);
	const header = '$effect(() => {';
	const headerStart = source.lastIndexOf(header, anchorIdx);
	assert.notEqual(headerStart, -1, `${header} not found before anchor in ${file}`);
	const braceStart = headerStart + header.length - 1;
	const braceEnd = matchBrace(source, braceStart);
	return source.slice(headerStart + '$effect('.length, braceEnd + 1);
}

function compileTs(code) {
	return transformSync(code, { loader: 'ts' }).code;
}

/** Run the real TryerLanes fetch effect; returns { start, cleanup, get lanes, get failure }. */
function makeLanesFetchHarness(getLanes) {
	const arrowSrc = extractEffectArrow(
		TRYER_LANES_SOURCE,
		'TryerLanes.svelte',
		'getLanes(jobId, candidateId, columns)'
	);
	const compiled = compileTs(arrowSrc);
	const factory = new Function(
		'getLanes',
		`
		let lanes = null;
		let failure = null;
		let jobId;
		let candidateId;
		const __effect__ = ${compiled};
		let cleanup = null;
		function run() {
			if (cleanup) cleanup();
			cleanup = __effect__();
		}
		return {
			start(j, c) {
				jobId = j;
				candidateId = c;
				run();
				return cleanup;
			},
			get lanes() { return lanes; },
			get failure() { return failure; }
		};
		`
	);
	return factory(getLanes);
}

test('TryerLanes fetch effect: stale success from candidate A does not overwrite B after cleanup', async () => {
	const deferred = new Map();
	function getLanes(j, c) {
		const key = `${j}:${c}`;
		let d = deferred.get(key);
		if (!d) {
			d = {};
			d.promise = new Promise((resolve, reject) => {
				d.resolve = resolve;
				d.reject = reject;
			});
			deferred.set(key, d);
		}
		return d.promise;
	}
	const h = makeLanesFetchHarness(getLanes);
	const bodyA = { duration_s: 1, decks: [] };
	const bodyB = { duration_s: 2, decks: [] };

	let cleanupA = h.start('job', 'A');
	cleanupA();
	h.start('job', 'B');

	const dA = deferred.get('job:A');
	const dB = deferred.get('job:B');
	dB.resolve(bodyB);
	await dB.promise;
	dA.resolve(bodyA);
	await dA.promise;

	assert.equal(
		h.lanes,
		bodyB,
		"if A's late success is kept, the card shows the superseded candidate"
	);
	assert.equal(h.failure, null);
});

test('TryerLanes fetch effect: failure for superseded candidate A does not set failure for B', async () => {
	const deferred = new Map();
	function getLanes(j, c) {
		const key = `${j}:${c}`;
		let d = deferred.get(key);
		if (!d) {
			d = {};
			d.promise = new Promise((resolve, reject) => {
				d.resolve = resolve;
				d.reject = reject;
			});
			deferred.set(key, d);
		}
		return d.promise;
	}
	const h = makeLanesFetchHarness(getLanes);
	let cleanupA = h.start('job', 'A');
	cleanupA();
	h.start('job', 'B');

	const dA = deferred.get('job:A');
	const dB = deferred.get('job:B');
	dA.reject(new Error('lanes boom'));
	try {
		await dA.promise;
	} catch {
		// expected
	}
	assert.equal(h.failure, null, 'a superseded failure must not set the error');
	assert.equal(h.lanes, null, 'B still loading');
	dB.resolve({ duration_s: 2, decks: [] });
	await dB.promise;
	assert.equal(h.lanes.duration_s, 2);
});

test('TryerLanes fetch effect: current success still applies when the run is not cancelled', async () => {
	const deferred = new Map();
	function getLanes(j, c) {
		const key = `${j}:${c}`;
		let d = deferred.get(key);
		if (!d) {
			d = {};
			d.promise = new Promise((resolve, reject) => {
				d.resolve = resolve;
				d.reject = reject;
			});
			deferred.set(key, d);
		}
		return d.promise;
	}
	const h = makeLanesFetchHarness(getLanes);
	h.start('job', 'A');
	const bodyA = { duration_s: 1, decks: [] };
	deferred.get('job:A').resolve(bodyA);
	await deferred.get('job:A').promise;
	assert.equal(h.lanes, bodyA);
	assert.equal(h.failure, null);
});

test('TryerLanes fetch effect: current failure still applies when the run is not cancelled', async () => {
	const deferred = new Map();
	function getLanes(j, c) {
		const key = `${j}:${c}`;
		let d = deferred.get(key);
		if (!d) {
			d = {};
			d.promise = new Promise((resolve, reject) => {
				d.resolve = resolve;
				d.reject = reject;
			});
			deferred.set(key, d);
		}
		return d.promise;
	}
	const h = makeLanesFetchHarness(getLanes);
	h.start('job', 'A');
	deferred.get('job:A').reject(new Error('lanes boom'));
	try {
		await deferred.get('job:A').promise;
	} catch {
		// expected
	}
	assert.equal(h.failure, 'lanes boom');
});

test('TryerLanes fetch effect: stale success from job-a does not overwrite job-b after cleanup', async () => {
	const deferred = new Map();
	function getLanes(j, c) {
		const key = `${j}:${c}`;
		let d = deferred.get(key);
		if (!d) {
			d = {};
			d.promise = new Promise((resolve, reject) => {
				d.resolve = resolve;
				d.reject = reject;
			});
			deferred.set(key, d);
		}
		return d.promise;
	}
	const h = makeLanesFetchHarness(getLanes);
	const bodyJobA = { duration_s: 1, decks: [] };
	const bodyJobB = { duration_s: 3, decks: [] };

	let cleanup = h.start('job-a', 'A');
	cleanup();
	h.start('job-b', 'A');

	const dA = deferred.get('job-a:A');
	const dB = deferred.get('job-b:A');
	dB.resolve(bodyJobB);
	await dB.promise;
	dA.resolve(bodyJobA);
	await dA.promise;

	assert.equal(h.lanes, bodyJobB);
	assert.equal(h.failure, null);
});
