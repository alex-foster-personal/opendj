/**
 * HEALTH-8304, HEALTH-8304b, HEALTH-8304c: the liveness and coverage lights are honest and calm.
 *
 * the maintainer, Tue 3 Nov 2026, on the packaged app right after a restart: Frontend
 * read "dev server not answering - Fetch is aborted", Backend "engine not
 * answering - Fetch is aborted" and every coverage dot "unknown - coverage
 * request timed out after 15 s", while the engine was only busy booting.
 *
 * [if] a probe times out inside the boot window before the engine has ever answered [then ⛔️] the dot must be amber "starting up", not red
 * [if] a probe fails fewer than FAILURES_BEFORE_DOWN times in a row [then ⛔️] the dot must be amber "retrying", not red
 * [if] a probe keeps failing [then ⛔️] the dot must turn red, and one answer must turn it green again
 * [if] the page is served by the engine (packaged) [then ⛔️] the row must read App UI, served by the engine, never "dev server"
 * [if] a coverage read times out [then ⛔️] the dot must be amber "still counting", keeping any verdict already painted
 * [if] WebKit rejects a timed-out fetch with AbortError "Fetch is aborted" [then ⛔️] the probe must report a timeout
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const dots = await loadTypeScriptModule('src/lib/rb/liveness-dots.ts');
const probes = await loadTypeScriptModule('src/lib/rb/browser-health-probes.ts', {
	viteApiBase: 'https://liveness.example.test'
});

const TIMEOUT = { ok: false, timedOut: true, why: 'no answer within 2 s' };
const REFUSED = { ok: false, timedOut: false, why: 'Load failed' };
const OK = { ok: true };

test('a timeout during the boot window is amber "starting up", however many times it repeats', () => {
	const tracker = dots.createLivenessTracker('Backend', 'engine', 0);
	for (let i = 0; i < 10; i += 1) {
		const dot = tracker.observe(TIMEOUT, 2500 * i);
		assert.equal(dot.state, 'waiting');
		assert.match(dot.detail, /^engine starting up - busy booting, retrying/);
		assert.doesNotMatch(dot.detail, /Fetch is aborted/);
	}
});

test('after the boot window, failures are amber "retrying" until they repeat, then red; one answer recovers', () => {
	const tracker = dots.createLivenessTracker('Backend', 'engine', 0);
	const late = dots.STARTUP_GRACE_MS + 1;
	const states = [];
	for (let i = 0; i < dots.FAILURES_BEFORE_DOWN; i += 1)
		states.push(tracker.observe(TIMEOUT, late + i).state);
	assert.deepEqual(states, [...Array(dots.FAILURES_BEFORE_DOWN - 1).fill('waiting'), 'error']);
	const recovered = tracker.observe(OK, late + 10);
	assert.deepEqual(recovered, {
		label: 'Backend',
		state: 'complete',
		detail: 'engine online'
	});
	assert.equal(
		tracker.observe(TIMEOUT, late + 11).state,
		'waiting',
		'the count restarts after an answer'
	);
});

test('a non-timeout failure is never "starting up" and turns red only after repeated failures', () => {
	const tracker = dots.createLivenessTracker('Frontend', 'dev server', 0);
	const first = tracker.observe(REFUSED, 0);
	assert.equal(first.state, 'waiting');
	assert.match(
		first.detail,
		/^dev server slow to answer - retrying, 1 of 3 tries missed \(Load failed\)$/
	);
	tracker.observe(REFUSED, 1);
	const third = tracker.observe(REFUSED, 2);
	assert.deepEqual(third, {
		label: 'Frontend',
		state: 'error',
		detail: 'dev server not answering - Load failed (3 tries in a row)'
	});
});

test('once the engine has answered, a later timeout inside the boot window is not called booting', () => {
	const tracker = dots.createLivenessTracker('Backend', 'engine', 0);
	tracker.observe(OK, 0);
	assert.match(tracker.observe(TIMEOUT, 1000).detail, /^engine slow to answer/);
});

test('the packaged App UI row mirrors the Backend dot and never mentions a dev server', () => {
	const cases = [
		[{ label: 'Backend', state: 'complete', detail: 'engine online' }, 'complete'],
		[{ label: 'Backend', state: 'waiting', detail: 'x' }, 'waiting'],
		[{ label: 'Backend', state: 'error', detail: 'x' }, 'error'],
		[{ label: 'Backend', state: 'loading', detail: 'x' }, 'loading']
	];
	for (const [backend, state] of cases) {
		const dot = dots.appUiDot(backend);
		assert.equal(dot.label, 'App UI');
		assert.equal(dot.state, state);
		assert.match(dot.detail, /^served by the engine/);
		assert.doesNotMatch(dot.detail, /dev server/);
	}
});

test('a coverage timeout is amber "still counting" before any verdict, and keeps a painted verdict', () => {
	const why = 'coverage request timed out after 15 s';
	const loading = {
		label: 'Stems completion',
		state: 'loading',
		detail: 'checking stems coverage'
	};
	const counting = dots.coverageTimeoutDot(loading, 'Stems completion', why);
	assert.equal(counting.state, 'waiting');
	assert.match(counting.detail, /^still counting - the engine is busy/);
	assert.equal(dots.coverageTimeoutDot(counting, 'Stems completion', why).state, 'waiting');

	const verdict = {
		label: 'Stems completion',
		state: 'incomplete',
		detail: '90/100 done'
	};
	const kept = dots.coverageTimeoutDot(verdict, 'Stems completion', why);
	assert.deepEqual(kept, {
		label: 'Stems completion',
		state: 'incomplete',
		detail: '90/100 done (re-check timed out, retrying)'
	});
	assert.equal(
		dots.coverageTimeoutDot(kept, 'Stems completion', why).detail,
		kept.detail,
		'the mark is not stacked'
	);
});

test('probe timeout detection covers Chromium TimeoutError and WebKit AbortError, nothing else', () => {
	assert.equal(
		probes.isProbeTimeout(new DOMException('The operation timed out.', 'TimeoutError')),
		true
	);
	assert.equal(probes.isProbeTimeout(new DOMException('Fetch is aborted', 'AbortError')), true);
	assert.equal(probes.isProbeTimeout(new TypeError('Load failed')), false);
	assert.equal(probes.isProbeTimeout('nope'), false);
});

test('_pingFrontend reports a WebKit "Fetch is aborted" rejection as a timeout, and HTTP errors as failures', async () => {
	const originalFetch = globalThis.fetch;
	const originalWindow = globalThis.window;
	globalThis.window = { location: { origin: 'https://liveness.example.test' } };
	try {
		globalThis.fetch = async () => {
			throw new DOMException('Fetch is aborted', 'AbortError');
		};
		assert.deepEqual(await probes._pingFrontend(), {
			ok: false,
			timedOut: true,
			why: 'no answer within 2 s'
		});
		globalThis.fetch = async () => new Response('', { status: 502 });
		assert.deepEqual(await probes._pingFrontend(), {
			ok: false,
			timedOut: false,
			why: 'HTTP 502'
		});
		globalThis.fetch = async () => new Response('', { status: 200 });
		assert.deepEqual(await probes._pingFrontend(), { ok: true });
	} finally {
		globalThis.fetch = originalFetch;
		globalThis.window = originalWindow;
	}
});

test('a coverage read that times out throws the typed CoverageTimeoutError the panel tests for', async () => {
	const ingest = await loadTypeScriptModule('src/lib/rb/api-ingest.ts', {
		viteApiBase: 'https://liveness.example.test'
	});
	const originalFetch = globalThis.fetch;
	globalThis.fetch = (_url, init) =>
		new Promise((_resolve, reject) => {
			init.signal.addEventListener('abort', () =>
				reject(new DOMException('Fetch is aborted', 'AbortError'))
			);
		});
	try {
		await assert.rejects(
			() => ingest.getIngestCoverage({ cached: true, timeoutMs: 20 }),
			(error) =>
				error.name === 'CoverageTimeoutError' && /timed out after 0.02 s/.test(error.message)
		);
	} finally {
		globalThis.fetch = originalFetch;
	}
});

test('BrowserPanel: a production build shows App UI from the Backend dot and sends no dev-server ping', async () => {
	const { readFileSync } = await import('node:fs');
	const src = readFileSync(
		new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url),
		'utf8'
	);
	assert.match(src, /const pageServedByEngine = !import\.meta\.env\.DEV;/);
	assert.match(src, /pageServedByEngine \? appUiDot\(backendOnline\) : devServerOnline/);
	assert.match(src, /pageServedByEngine \? null : _pingFrontend\(\)/);
	assert.match(src, /if \(isCoverageTimeout\(error\)\) \{/);
	assert.match(src, /class:waiting=\{dot\.state === 'waiting'\}/);
});
