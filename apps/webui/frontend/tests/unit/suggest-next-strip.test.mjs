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
 * - every load control exposes the current destination channel, preserves its
 *   explanation, and explicitly disables itself with no free deck
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

test('candidate load controls retain their channel and explanation in accessible labels', () => {
	assert.match(source, /targetLabel: string \| null;/);
	assert.doesNotMatch(source, /from '\$lib\/rb\/deck-slots'/);
	assert.match(source, /` - \$\{candidate\.explain_text\}`/);
	assert.match(source, /title=\{_loadControlLabel\(cand, false\)\}/);
	assert.match(source, /aria-label=\{_loadControlLabel\(cand, true\)\}/);
});

test('candidate controls explicitly disable when no deck is free', () => {
	assert.match(source, /no free deck available/);
	assert.match(source, /disabled=\{targetLabel === null\}/);
});

test('BrowserPanel wires the current lowest free deck into SuggestNextStrip', () => {
	const browser = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)),
		'utf8'
	);
	assert.match(browser, /const suggestTargetDeck = \$derived\(_lowestFreeDeck\(\)\);/);
	assert.match(browser, /<SuggestNextStrip[\s\S]*?targetLabel=\{suggestTargetDeck === null \? null : `CH \$\{suggestTargetDeck\}`\}[\s\S]*?onload=/);
	assert.match(browser, /function loadSuggest\(sid: string/);
});

test('play label and dispatch share the reservation-aware picker', () => {
	const browser = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)), 'utf8'
	);
	assert.match(browser, /const suggestPlayTargetDeck = \$derived\(_pickDoubleDeckTarget\(\)\.deck\);/);
	assert.match(browser, /playTargetLabel=\{suggestPlayTargetDeck === null \? null : `CH \$\{suggestPlayTargetDeck\}`\}/);
	assert.match(browser, /const result = _pickDoubleDeckTarget\(opts\);/);
	assert.match(source, /const destination = \(play \? playTargetLabel : targetLabel\)/);
	assert.match(source, /disabled=\{playTargetLabel === null\}/);
});
