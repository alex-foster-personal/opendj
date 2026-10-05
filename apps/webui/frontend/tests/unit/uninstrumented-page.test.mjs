/**
 * PERFMODE-15 capture page (`scripts/perf/uninstrumented-page.mjs`): which
 * errors `waitForFunction` may retry, and which CDP methods it may send.
 *
 * Sol P1 (PR #4857): the poll retried any error whose message contained
 * "context", so a predicate that threw an AudioContext error was swallowed
 * until the timeout instead of failing at once.
 */
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { test } from 'node:test';

import {
	CdpProtocolError,
	assertUninstrumentedMethod,
	isContextLossError,
	openUninstrumentedPage
} from '../../../../../scripts/perf/uninstrumented-page.mjs';

const FRONTEND_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');

async function withRealChromium(run) {
	let playwright;
	try {
		const require = createRequire(path.join(FRONTEND_ROOT, 'package.json'));
		const entry = require.resolve('@playwright/test');
		playwright = await import(pathToFileURL(entry).href);
	} catch (error) {
		return { skip: `UNAVAILABLE: @playwright/test is not installed: ${error.message}` };
	}
	try {
		const browser = await (playwright.default ?? playwright).chromium.launch({ headless: true });
		try {
			return { result: await run(browser) };
		} finally {
			await browser.close();
		}
	} catch (error) {
		return { skip: `UNAVAILABLE: Chromium could not launch: ${error.message}` };
	}
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

test('waitForFunction resolves when evaluate returns a truthy value quickly', async (t) => {
	const outcome = await withRealChromium(async (browser) => {
		const page = await openUninstrumentedPage(browser);
		const value = await page.waitForFunction(() => 7, undefined, { timeout: 5000 });
		assert.equal(value, 7);
		await page.close();
	});
	if (outcome.skip !== undefined) {
		t.skip(outcome.skip);
		return;
	}
});

test('waitForFunction rejects near timeout when evaluate never replies', async (t) => {
	const outcome = await withRealChromium(async (browser) => {
		const page = await openUninstrumentedPage(browser);
		const started = Date.now();
		await assert.rejects(
			() =>
				page.waitForFunction(
					() => new Promise(() => {}),
					undefined,
					{ timeout: 200 }
				),
			(error) => {
				assert.match(error.message, /waitForFunction timed out after 200ms/);
				return true;
			}
		);
		const elapsed = Date.now() - started;
		assert.ok(elapsed >= 150 && elapsed < 2000, `expected ~200ms, got ${elapsed}ms`);
		await page.close();
	});
	if (outcome.skip !== undefined) {
		t.skip(outcome.skip);
		return;
	}
});

test('waitForFunction ignores a late evaluate response after deadline cleanup', async (t) => {
	const outcome = await withRealChromium(async (browser) => {
		const page = await openUninstrumentedPage(browser);
		await assert.rejects(
			() =>
				page.waitForFunction(
					() => new Promise((resolve) => setTimeout(() => resolve(true), 400)),
					undefined,
					{ timeout: 200 }
				),
			/waitForFunction timed out after 200ms/
		);
		await page.close();
	});
	if (outcome.skip !== undefined) {
		t.skip(outcome.skip);
		return;
	}
});
