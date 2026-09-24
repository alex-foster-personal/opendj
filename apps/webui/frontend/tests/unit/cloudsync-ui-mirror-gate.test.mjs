/**
 * Runtime behavior of fetchUiMirrorForGate (cloudsync-view.ts), issue #3531.
 *
 * `GET /api/v1/state/ui-mirror` 409s on every app-shell route besides
 * `/performance` (there is no live performance mirror to report there) - that
 * is EXPECTED and must resolve to null, not disable Sync / stall the Status
 * tab from every other route. Any OTHER failure (network error, 500, ...)
 * must still propagate, so a transient failure never widens the sync gate by
 * being misread as "no deck playing".
 *
 * Regression lines:
 * - if a 409 stops resolving to null then Sync now/Force sync are disabled
 *   and the Status tab is stuck on "Loading status..." on every route
 *   except /performance (the P1 this file guards against, PR #3551 review)
 * - if a non-409 failure stops propagating then a transient error is read as
 *   "no deck playing" and can widen the sync gate around a real playing deck
 *   (the P2 the fail-closed fix originally guarded against)
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://ui-mirror-gate.example.test';

let view;
let originalFetch;

before(async () => {
	view = await loadTypeScriptModule('src/lib/components/cloudsync/cloudsync-view.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('a 409 (no performance mirror mounted) resolves to null, not a thrown error', async () => {
	globalThis.fetch = async () => new Response(null, { status: 409, statusText: 'Conflict' });

	const result = await view.fetchUiMirrorForGate();

	assert.equal(result, null);
});

test('a non-409 failure still propagates (fail closed)', async () => {
	globalThis.fetch = async () => new Response(null, { status: 500, statusText: 'Server Error' });

	await assert.rejects(() => view.fetchUiMirrorForGate());
});

test('a 2xx response returns the decks payload', async () => {
	globalThis.fetch = async () =>
		new Response(JSON.stringify({ decks: { 1: { playing: true } } }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});

	const result = await view.fetchUiMirrorForGate();

	assert.deepEqual(result, { decks: { 1: { playing: true } } });
});
