/**
 * Library job queue capability refusal, RENDERED (issue #13222).
 *
 * (private path), Queue panel: the panel is inert only
 * when the daemon does not offer the /api/v1/library-jobs routes, and it must
 * never imply an enqueue succeeded when it was refused. Whether the routes are
 * offered is the capability probe's answer (capabilities.flavor from GET
 * /api/v1/health), not a guess from a 404 body. This drives the real probe and
 * the real libraryJobsStore with a fake fetch, then renders the real
 * LibraryJobQueuePanel with Svelte's server renderer and reads the markup.
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const ENTRY = [
	"export { default as LibraryJobQueuePanel } from '$lib/components/rb/library-jobs/LibraryJobQueuePanel.svelte';",
	"export { libraryJobsStore } from '$lib/rb/library-jobs-store.svelte';",
	"export { capabilities } from '$lib/api/capabilities.svelte';",
	"export { jobProgress } from '$lib/rb/job-progress.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

let mod;
let originalFetch;
let originalConsoleError;
let calls;
let errors;

/** A JSON response with the given status. */
function jsonResponse(body, status = 200) {
	return new Response(JSON.stringify(body), {
		status,
		statusText: status === 404 ? 'Not Found' : 'OK',
		headers: { 'content-type': 'application/json' }
	});
}

/** One pending stems job, shaped like the route's LibraryJobItem. */
function pendingItem(stableId) {
	return {
		lane: 'stems',
		stable_id: stableId,
		state: 'pending',
		reason: null,
		detail: null,
		position: 1,
		attempts: 0,
		enqueued_at: '2026-11-13T00:00:00Z',
		started_at: null,
		finished_at: null
	};
}

/** The engine's health body: all three handshake fields, so flavor 'engine'. */
const ENGINE_HEALTH = { status: 'ok', contract_rev: 'r1', engine_version: 'e1', boot_id: 'b1' };

/** Routes served: list answers with one pending stems job, enqueue echoes it. */
function servedFetch(request, init) {
	const url = typeof request === 'string' ? request : request.url;
	const method = init?.method ?? (typeof request === 'string' ? 'GET' : request.method);
	calls.push({ url, method });
	if (url.includes('/api/v1/health')) return jsonResponse(ENGINE_HEALTH);
	if (url.includes('/api/v1/library-jobs/enqueue')) {
		return jsonResponse({ lane: 'stems', items: [pendingItem('t1')], already_running: [] });
	}
	if (url.includes('/api/v1/library-jobs?')) {
		const lane = new URL(url).searchParams.get('lane');
		const items = lane === 'stems' ? [pendingItem('t1')] : [];
		return jsonResponse({ lane, items, counts: {} });
	}
	throw new Error(`unexpected fetch: ${url}`);
}

/** Probe the daemon with the given fetch, so capabilities.flavor is the probe's real verdict. */
async function probeWith(fakeFetch) {
	mod.capabilities._resetForTests();
	globalThis.fetch = async (request, init) => fakeFetch(request, init);
	await mod.capabilities.probe();
}

/** The daemon is not identified: the health probe fails, so flavor stays 'unknown'. */
async function leaveUnidentified() {
	await probeWith((request, init) => {
		const url = typeof request === 'string' ? request : request.url;
		if (url.includes('/api/v1/health')) throw new TypeError('fetch failed');
		return servedFetch(request, init);
	});
	globalThis.fetch = async (request, init) => servedFetch(request, init);
}

/** Render the open panel and return its markup. */
function renderPanel() {
	return mod.render(mod.LibraryJobQueuePanel, { props: { open: true } }).body;
}

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
	originalFetch = globalThis.fetch;
	originalConsoleError = console.error;
});

after(() => {
	// Hydrated pending jobs arm job-progress's ticker; clear them so the process can exit.
	mod.jobProgress.clear('t1');
	globalThis.fetch = originalFetch;
	console.error = originalConsoleError;
});

beforeEach(async () => {
	errors = [];
	console.error = (...args) => errors.push(args.map(String).join(' '));
	// Start every test from an identified engine so a refusal never leaks across tests.
	calls = [];
	await probeWith(servedFetch);
	assert.equal(mod.capabilities.flavor, 'engine');
	await mod.libraryJobsStore.refresh();
	calls = [];
});

test('routes offered: panel lists jobs with live controls and no refusal', async () => {
	// An identified daemon offers the queue, so the panel is interactive.
	await mod.libraryJobsStore.refresh();
	assert.equal(mod.libraryJobsStore.refusal, null);
	const html = renderPanel();
	assert.match(html, /data-testid="library-job-row"[^>]*data-stable-id="t1"/);
	assert.match(html, /t1 queued/);
	assert.match(html, />Cancel</);
	assert.doesNotMatch(html, /library-job-refusal/);
});

test('the legacy daemon offers the queue too', async () => {
	// routes/library_jobs.py mounts in the legacy app, which the engine wraps.
	await probeWith((request, init) => {
		const url = typeof request === 'string' ? request : request.url;
		if (url.includes('/api/v1/health')) return jsonResponse({ status: 'ok' });
		return servedFetch(request, init);
	});
	assert.equal(mod.capabilities.flavor, 'legacy');
	assert.equal(mod.libraryJobsStore.refusal, null);
	assert.equal(await mod.libraryJobsStore.enqueue('stems', ['t1']), null);
});

test('daemon not identified: panel is inert, says why, and lists nothing', async () => {
	// No capability verdict means no queue: the panel refuses instead of reading "No jobs".
	await leaveUnidentified();
	calls = [];
	await mod.libraryJobsStore.refresh();
	const refusal = mod.libraryJobsStore.refusal;
	assert.match(refusal ?? '', /daemon not identified/);
	assert.deepEqual(calls, [], 'a refused queue must not fire list requests');
	const html = renderPanel();
	assert.match(html, /data-testid="library-job-refusal"/);
	assert.equal(html.includes(refusal), true);
	assert.doesNotMatch(html, /No jobs/);
	assert.doesNotMatch(html, />Cancel</);
});

test('enqueue on an unoffered queue returns the refusal and sends nothing', async () => {
	// Invoking "do next" before the capability is known must not POST or report success.
	await leaveUnidentified();
	calls = [];
	const result = await mod.libraryJobsStore.enqueue('stems', ['t1', 't2']);
	assert.equal(result, mod.libraryJobsStore.refusal);
	assert.match(result ?? '', /daemon not identified/);
	assert.deepEqual(calls, []);
});

test('enqueue on an offered queue posts and resolves with no refusal', async () => {
	// The offered path still enqueues and rehydrates, so the gate does not block real work.
	const result = await mod.libraryJobsStore.enqueue('stems', ['t1']);
	assert.equal(result, null);
	assert.ok(calls.some((c) => c.url.includes('/library-jobs/enqueue') && c.method === 'POST'));
	assert.ok(calls.some((c) => c.url.includes('/library-jobs?lane=stems')));
});

test('track-menu "do next" tells the DJ when the queue refuses or the POST fails', async () => {
	// BrowserPanel passes pushToast as notify; a refused or failed order is never silent.
	const notes = [];
	const notify = (message, kind) => notes.push({ message, kind });
	await mod.libraryJobsStore.doNext('stems', ['t1'], notify);
	assert.deepEqual(notes, [], 'an accepted order must not raise a notice');

	globalThis.fetch = async (request, init) => {
		const url = typeof request === 'string' ? request : request.url;
		if (url.includes('/enqueue'))
			return jsonResponse({ detail: { code: 'x', message: 'disk full' } }, 500);
		return servedFetch(request, init);
	};
	await mod.libraryJobsStore.doNext('lyrics', ['t2'], notify);
	assert.equal(notes.length, 1);
	assert.equal(notes[0].kind, 'error');
	assert.match(notes[0].message, /Lyrics not queued: .*disk full/);

	await leaveUnidentified();
	await mod.libraryJobsStore.doNext('stems', ['t3'], notify);
	assert.equal(notes.length, 2);
	assert.match(notes[1].message, /Stems not queued: daemon not identified/);
});
