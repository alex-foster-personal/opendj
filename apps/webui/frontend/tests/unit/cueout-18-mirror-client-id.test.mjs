/**
 * CUEOUT-18: every mirror publish names the page that sent it, so the engine
 * can keep two open tabs' headphone reports apart (the route half is
 * tests/webui/test_performance_headphones_per_client.py).
 *
 * Source-shape assertions: buildUiMirror reads the live audio engine, which
 * node:test cannot construct.
 *
 * Regression lines:
 *   - if the published document carries no client_id then broken
 *   - if the id is re-made per publish instead of once per page load then broken
 *   - if the close request does not name the same client then broken
 *   - if the id needs a secure context (crypto.randomUUID) then the packaged shell cannot publish
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const SOURCE = readFileSync(
	fileURLToPath(new URL('../../src/lib/rb/ui-mirror.ts', import.meta.url)),
	'utf8'
);

test('the id is made once, at module scope, without a secure-context API', () => {
	assert.match(SOURCE, /^const MIRROR_CLIENT_ID = /m);
	assert.doesNotMatch(SOURCE, /randomUUID/);
});

test('the published document carries the id', () => {
	const build = SOURCE.slice(
		SOURCE.indexOf('export function buildUiMirror'),
		SOURCE.indexOf('export function installUiMirror')
	);
	assert.ok(build.length > 0, 'control: buildUiMirror was found');
	assert.match(build, /client_id: MIRROR_CLIENT_ID/);
});

test('the close request names the same client', () => {
	// A header, not a query string: the request URL stays exactly MIRROR_PATH,
	// which is what every existing route interception matches.
	assert.match(
		SOURCE,
		/fetch\(MIRROR_PATH, \{\s*method: 'DELETE',\s*keepalive: true,\s*headers: \{ 'x-opendj-client-id': MIRROR_CLIENT_ID \}\s*\}\)/
	);
});
