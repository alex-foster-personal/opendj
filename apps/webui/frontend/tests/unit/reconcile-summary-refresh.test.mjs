/**
 * HEALTH-15: the browser panel's reconcile summary reads, through the real
 * module BrowserPanel.svelte calls (no source slicing).
 *
 * Regression lines:
 *   - if a first scan still running paints counts or an error then broken
 *   - if a still-running scan does not ask to be re-read then broken
 *   - if a failed rescan quotes the last counts as current then broken
 *   - if a failed request is reported as still warming then broken
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://reconcile-summary.example.test';
let refresh;
let originalFetch;

before(async () => {
	refresh = await loadTypeScriptModule('src/lib/rb/reconcile-summary-refresh.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

function answer(body, status = 200) {
	globalThis.fetch = async () => Response.json(body, { status });
}

async function readOnce() {
	const applied = [];
	const outcome = await refresh.readReconcileSummaryOnce((read) => applied.push(read));
	return { applied, outcome };
}

const SUMMARY = { total_tracks: 10, total_broken: 2, orphan_broken: 0, playlists: [] };

// REQ: HEALTH-15
test('a first scan still running paints nothing and asks to be re-read', async () => {
	// [if] the engine answers 503 RECONCILE_SUMMARY_WARMING [then] nothing applies, re-ask, [else stop].
	answer({ detail: { code: 'RECONCILE_SUMMARY_WARMING', message: 'first scan running' } }, 503);
	const { applied, outcome } = await readOnce();
	assert.deepEqual(applied, []);
	assert.deepEqual(outcome, { ok: true, refreshing: true, refresh_error: null });
});

// REQ: HEALTH-15
test('a snapshot paints its counts, and a running rescan asks to be re-read', async () => {
	// [if] a snapshot arrives while a rescan runs [then] counts paint and re-ask, [else stop].
	answer({ ...SUMMARY, availability: null, age_s: 70, refreshing: true, refresh_error: null });
	const { applied, outcome } = await readOnce();
	assert.deepEqual(applied, [
		{ counts: { nonBroken: 8, broken: 2, availability: 'unknown' }, error: null }
	]);
	assert.deepEqual(outcome, { ok: true, refreshing: true, refresh_error: null });
});

// REQ: HEALTH-15
test('a failed rescan keeps the counts but says they are unchecked', async () => {
	// [if] refresh_error is set [then] the error names it beside the counts, [else stop].
	answer({ ...SUMMARY, age_s: 400, refreshing: false, refresh_error: 'OperationalError: disk I/O error' });
	const { applied, outcome } = await readOnce();
	assert.equal(applied.length, 1);
	assert.match(applied[0].error, /last library scan failed \(OperationalError: disk I\/O error\)/);
	assert.equal(outcome.refresh_error, 'OperationalError: disk I/O error');
});

// REQ: HEALTH-15
test('any other failure is an error, never "still warming"', async () => {
	// [if] the engine answers a plain 500 [then] the error applies and ok is false, [else stop].
	answer({ detail: { code: 'BOOM', message: 'engine fell over' } }, 500);
	const { applied, outcome } = await readOnce();
	assert.deepEqual(outcome, { ok: false });
	assert.equal(applied.length, 1);
	assert.equal(applied[0].counts, null);
	assert.match(applied[0].error, /engine fell over/);
});
