/**
 * Source-shape regression pinning SuggestNextStrip.svelte's transport
 * conversion onto the generated OpenAPI client.
 *
 * This is deliberately NOT a behavioral test: the repo has no component
 * mount infrastructure (no jsdom/happy-dom, and the esbuild test loader is
 * TypeScript-only, so a .svelte file cannot be imported here). The repo
 * idiom for .svelte assertions is source-level (see inert-controls.test.mjs).
 * What this pins:
 * - no raw fetch() call remains in the component
 * - the call goes through api.POST with the exact schema path literal
 * - the client import replaced the RB_API_BASE import
 * - the 422 insufficient-data outcome is mapped off ApiError
 * - the body carries explain: false (required by the generated SuggestNextIn;
 *   false is the server default the old raw fetch relied on)
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const COMPONENT = fileURLToPath(
	new URL('../../src/lib/components/rb/SuggestNextStrip.svelte', import.meta.url)
);
const source = readFileSync(COMPONENT, 'utf8');

test('no raw fetch call remains in the component', () => {
	assert.doesNotMatch(source, /(^|[^A-Za-z0-9_])fetch\(/);
});

test('the suggest-next call goes through the generated client with the schema path', () => {
	assert.match(source, /api\.POST\('\/api\/v1\/copilot\/suggest-next'/);
	assert.match(source, /import \{ ApiError, api, unwrap \} from '\$lib\/api\/client';/);
	assert.doesNotMatch(source, /RB_API_BASE/);
});

test('the 422 insufficient-data outcome is mapped off ApiError', () => {
	assert.match(source, /e instanceof ApiError && e\.status === 422/);
});

test('the request body sends the explicit explain: false server default', () => {
	assert.match(source, /explain: false/);
});
