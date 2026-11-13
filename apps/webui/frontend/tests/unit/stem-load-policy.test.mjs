/**
 * Issue #12446: PERFBATCH-06 envelope -- precomputed stem bundle before live-generation fallback.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let policy;

before(async () => {
	policy = await loadTypeScriptModule('src/lib/rb/stem-load-policy.ts');
});

function liveGenerationSpy() {
	let calls = 0;
	return {
		fn: () => {
			calls += 1;
			return false;
		},
		callCount: () => calls
	};
}

test('resolveStemLoadPolicy: ready precomputed bundle wins over live-generation', () => {
	const fallback = liveGenerationSpy();
	assert.equal(policy.resolveStemLoadPolicy({ status: 'ready' }, fallback.fn), 'use-precomputed');
	assert.equal(fallback.callCount(), 0);
});

test('resolveStemLoadPolicy: hydrating cloud bundle awaits precomputed without fallback', () => {
	const fallback = liveGenerationSpy();
	assert.equal(
		policy.resolveStemLoadPolicy({ status: 'hydrating' }, fallback.fn),
		'await-precomputed'
	);
	assert.equal(fallback.callCount(), 0);
});

test('resolveStemLoadPolicy: unavailable may invoke live-generation fallback once', () => {
	const fallback = liveGenerationSpy();
	assert.equal(
		policy.resolveStemLoadPolicy({ status: 'unavailable', error: 'no bundle' }, fallback.fn),
		'consider-live-generation-fallback'
	);
	assert.equal(fallback.callCount(), 1);
});

test('resolveStemLoadPolicy: invalid bundle rejects without fallback', () => {
	const fallback = liveGenerationSpy();
	assert.equal(
		policy.resolveStemLoadPolicy({ status: 'invalid', error: 'bad manifest' }, fallback.fn),
		'reject-loud'
	);
	assert.equal(fallback.callCount(), 0);
});

test('resolveStemLoadPolicy: stale deck load (null probe) aborts without fallback', () => {
	const fallback = liveGenerationSpy();
	assert.equal(policy.resolveStemLoadPolicy(null, fallback.fn), 'aborted-stale');
	assert.equal(fallback.callCount(), 0);
});
