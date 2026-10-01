/**
 * PERFMODE-15 capture page (`scripts/perf/uninstrumented-page.mjs`): which
 * errors `waitForFunction` may retry, and which CDP methods it may send.
 *
 * Sol P1 (PR #4857): the poll retried any error whose message contained
 * "context", so a predicate that threw an AudioContext error was swallowed
 * until the timeout instead of failing at once.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	CdpProtocolError,
	assertUninstrumentedMethod,
	isContextLossError
} from '../../../../../scripts/perf/uninstrumented-page.mjs';

test('a protocol-level context loss during navigation is retryable', () => {
	assert.equal(isContextLossError(new CdpProtocolError('Runtime.evaluate', 'Execution context was destroyed.')), true);
	assert.equal(
		isContextLossError(new CdpProtocolError('Runtime.evaluate', 'Cannot find default execution context')),
		true
	);
});

test('a page exception that mentions a context is not retryable', () => {
	assert.equal(isContextLossError(new Error('NotAllowedError: AudioContext was not allowed to start')), false);
	assert.equal(isContextLossError(new Error('Execution context was destroyed.')), false);
});

test('any other protocol failure is not retryable', () => {
	assert.equal(isContextLossError(new CdpProtocolError('Runtime.evaluate', 'Internal error')), false);
});

test('the page refuses the Network domain and allows Runtime', () => {
	assert.throws(() => assertUninstrumentedMethod('Network.enable'), /refuses Network\.enable/);
	assert.doesNotThrow(() => assertUninstrumentedMethod('Runtime.evaluate'));
});
