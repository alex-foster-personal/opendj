/**
 * V1 punch list: the /queues, /pairings and /track/[stable_id] routes settle
 * visibly when their fetch fails, and the track notes field saves only real
 * edits.
 *
 * Before: each route awaited its fetch in onMount with no catch. A failed
 * queue or pairings request left a blank page; an unknown track id left
 * "Loading..." on screen forever; and every blur of the notes field sent a
 * PATCH (and toasted "Saved") even when nothing had been typed.
 *
 * Regression lines:
 * - if a 404 from the real track endpoint is not classified "not-found" then
 *   an unknown id is reported as a generic failure (or never settles)
 * - if an empty error message renders as an empty reason then the user sees
 *   "Could not load:" followed by nothing
 * - if an unchanged notes blur counts as a change then focus-and-leave writes
 * - if a route drops its catch, its retry, or its empty state then the page
 *   goes back to failing silently
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { after, before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://route-load.example.test';
const read = (p) => readFileSync(fileURLToPath(new URL(`../../${p}`, import.meta.url)), 'utf8');

let helpers;
let api;
let originalFetch;
before(async () => {
	helpers = await loadTypeScriptModule('src/lib/route-load-state.ts', { viteApiBase: API_BASE });
	api = await loadTypeScriptModule('src/lib/api.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
});
after(() => {
	globalThis.fetch = originalFetch;
});

describe('routeLoadError over the real track client', () => {
	it('an unknown track id (404) is "not-found" and carries the server reason', async () => {
		globalThis.fetch = async () =>
			Response.json({ error: 'not_found', message: 'track not found: nope' }, { status: 404 });
		const exc = await api.getTrack('nope').then(
			() => assert.fail('a 404 must reject'),
			(e) => e
		);
		const outcome = helpers.routeLoadError(exc);
		assert.equal(outcome.kind, 'not-found');
		assert.ok(outcome.message.length > 0, 'the reason must not be empty');
	});

	it('a server error is "error", not "not-found"', async () => {
		globalThis.fetch = async () =>
			Response.json({ detail: { code: 'BOOM', message: 'state.db locked' } }, { status: 500 });
		const exc = await api.getTrack('x').then(
			() => assert.fail('a 500 must reject'),
			(e) => e
		);
		const outcome = helpers.routeLoadError(exc);
		assert.equal(outcome.kind, 'error');
		assert.match(outcome.message, /state\.db locked/);
	});

	it('a network failure is "error" with its own words', () => {
		const outcome = helpers.routeLoadError(new TypeError('Failed to fetch'));
		assert.deepEqual(outcome, { kind: 'error', message: 'Failed to fetch' });
	});
});

describe('describeLoadError', () => {
	it('never returns an empty reason', () => {
		for (const exc of [new Error(''), '', '   ', null, undefined]) {
			assert.equal(helpers.describeLoadError(exc), 'request failed with no error message');
		}
		assert.equal(helpers.describeLoadError(new Error('boom')), 'boom');
		assert.equal(helpers.describeLoadError('plain words'), 'plain words');
	});
});

describe('notesChanged', () => {
	it('an unchanged blur is not an edit', () => {
		assert.equal(helpers.notesChanged('same', 'same'), false);
		assert.equal(helpers.notesChanged(null, ''), false);
		assert.equal(helpers.notesChanged(undefined, ''), false);
	});

	it('a real edit is an edit, including clearing the notes', () => {
		assert.equal(helpers.notesChanged('old', 'new'), true);
		assert.equal(helpers.notesChanged(null, 'first note'), true);
		assert.equal(helpers.notesChanged('to be cleared', ''), true);
	});
});

describe('the routes use these outcomes', () => {
	it('/queues catches its load, shows the reason with a retry, and has an empty state', () => {
		const src = read('src/routes/queues/+page.svelte');
		assert.match(src, /catch \(exc\)[\s\S]{0,120}loadError = describeLoadError\(exc\)/);
		assert.match(src, /Could not load this queue: \{loadError\}/);
		assert.match(src, /onclick=\{\(\) => void load\(\)\}>Retry</);
		assert.match(src, /This queue is empty/);
	});

	it('/pairings catches its load, shows the reason with a retry, and has empty states', () => {
		const src = read('src/routes/pairings/+page.svelte');
		assert.match(src, /catch \(exc\)[\s\S]{0,120}loadError = describeLoadError\(exc\)/);
		assert.match(src, /Could not load pairings: \{loadError\}/);
		assert.match(src, /onclick=\{\(\) => void load\(\)\}>Retry</);
		assert.match(src, /No pairings yet\. Add your first one above\./);
		assert.match(src, /No \$\{source\} pairings/);
	});

	it('/track settles to not-found or an error instead of loading forever', () => {
		const src = read('src/routes/track/[stable_id]/+page.svelte');
		assert.match(src, /catch \(exc\) \{\s*loadError = routeLoadError\(exc\);/);
		assert.match(src, /\{:else if loadError !== null\}[\s\S]*Track not found[\s\S]*\{:else\}\s*<p>Loading\.\.\.<\/p>/);
	});

	it('/track saves notes on blur only when they changed, and shows a failed save', () => {
		const src = read('src/routes/track/[stable_id]/+page.svelte');
		assert.match(src, /onblur=\{\(e\) => saveNotesOnBlur\(/);
		assert.match(src, /if \(!track \|\| !notesChanged\(track\.notes, next\)\) return;/);
		assert.doesNotMatch(src, /onblur=\{\(e\) => applyPatch\(/);
		assert.match(src, /Notes not saved: \{notesError\}/);
	});
});
