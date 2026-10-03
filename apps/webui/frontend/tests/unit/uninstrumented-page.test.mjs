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
	isContextLossError,
	openUninstrumentedPage
} from '../../../../../scripts/perf/uninstrumented-page.mjs';

/** Minimal browser + browser CDP session for `openUninstrumentedPage` unit tests. */
function createFakeCdpBrowser(onTargetMessage) {
	const messageHandlers = [];
	const sessionId = 'fake-target-session';

	const browserSession = {
		async send(method, params = {}) {
			if (method === 'Target.createBrowserContext') {
				return { browserContextId: 'fake-context' };
			}
			if (method === 'Target.createTarget') {
				return { targetId: 'fake-target' };
			}
			if (method === 'Target.attachToTarget') {
				return { sessionId };
			}
			if (method === 'Target.sendMessageToTarget') {
				const request = JSON.parse(params.message);
				const deliver = (response) => {
					for (const handler of messageHandlers) {
						handler({ sessionId, message: JSON.stringify(response) });
					}
				};
				onTargetMessage(request, deliver);
				return {};
			}
			if (method === 'Target.disposeBrowserContext') {
				return {};
			}
			throw new Error(`unexpected browserSession.send: ${method}`);
		},
		on(event, handler) {
			if (event === 'Target.receivedMessageFromTarget') {
				messageHandlers.push(handler);
			}
		},
		async detach() {}
	};

	return {
		async newBrowserCDPSession() {
			return browserSession;
		},
		on() {}
	};
}

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

test('waitForFunction resolves when evaluate returns a truthy value quickly', async () => {
	let evaluateCalls = 0;
	const browser = createFakeCdpBrowser((request, deliver) => {
		if (request.method !== 'Runtime.evaluate') return;
		evaluateCalls += 1;
		deliver({ id: request.id, result: { result: { value: 7 } } });
	});
	const page = await openUninstrumentedPage(browser);
	const value = await page.waitForFunction(() => 7, undefined, { timeout: 5000 });
	assert.equal(value, 7);
	assert.equal(evaluateCalls, 1);
});

test('waitForFunction rejects near timeout when evaluate never replies', async () => {
	const browser = createFakeCdpBrowser((request) => {
		if (request.method === 'Runtime.evaluate') {
			// Never deliver: simulates Runtime.evaluate with awaitPromise that never settles.
		}
	});
	const page = await openUninstrumentedPage(browser);
	const started = Date.now();
	await assert.rejects(
		() => page.waitForFunction(() => false, undefined, { timeout: 200 }),
		(error) => {
			assert.match(error.message, /waitForFunction timed out after 200ms/);
			return true;
		}
	);
	const elapsed = Date.now() - started;
	assert.ok(elapsed >= 150 && elapsed < 2000, `expected ~200ms, got ${elapsed}ms`);
});

test('waitForFunction ignores a late evaluate response after deadline cleanup', async () => {
	let deliverLate;
	const browser = createFakeCdpBrowser((request, deliver) => {
		if (request.method !== 'Runtime.evaluate') return;
		deliverLate = () => deliver({ id: request.id, result: { result: { value: true } } });
	});
	const page = await openUninstrumentedPage(browser);
	await assert.rejects(() => page.waitForFunction(() => false, undefined, { timeout: 200 }));
	assert.equal(typeof deliverLate, 'function');
	deliverLate();
});
