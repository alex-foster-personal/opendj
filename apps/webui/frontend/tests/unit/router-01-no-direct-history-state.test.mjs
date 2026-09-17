// requirement: ROUTER-01 (history entries go through the app router, never the raw browser API)
// [if] a route or component rewrites the address bar itself [then] the router desyncs and warns on every deck load
// [if] the session leaf imports the router's virtual module [then] node unit tests cannot load it at all
// (seen live Wed 16 Sep 2026: every /performance load logged the router's own warning about this)
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const FRONTEND = new URL('../../', import.meta.url);
const RAW_HISTORY = /window\.history\.(replaceState|pushState)/;
const ROUTER_IMPORT = /from '\$app\/navigation'/;
const ROUTED = ['src/routes/performance/+page.svelte', 'src/lib/components/rb/BrowserPanel.svelte'];

function read(relative) {
	return readFileSync(new URL(relative, FRONTEND), 'utf8');
}

for (const relative of ROUTED) {
	test(`if ${relative} writes history itself then the router warns and back/forward breaks`, () => {
		const source = read(relative);
		assert.doesNotMatch(source, RAW_HISTORY, 'use the router helpers instead of the raw browser API');
		assert.match(source, ROUTER_IMPORT, 'the router helpers must be imported');
	});
}

test('if the session leaf reaches for the router itself then node unit tests cannot load it', () => {
	const source = read('src/lib/rb/performance-session.svelte.ts');
	assert.doesNotMatch(source, RAW_HISTORY, 'the route injects the router helper instead');
	assert.doesNotMatch(source, /\$app\/navigation/, 'that virtual module does not resolve outside the framework');
});
