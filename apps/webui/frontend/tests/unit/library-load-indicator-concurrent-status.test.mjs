/**
 * Issue #1688 / PR #1672 P2: one overlay owns in-flight search AND load.
 *
 * Source-scans cannot prove concurrent markup. These compile the real
 * LibraryLoadIndicator and run svelte's SSR renderer over it, so every
 * assertion is about markup the component actually produced from props.
 *
 * WHAT A PASS HERE DOES NOT COVER: there is no DOM in this suite, so this
 * proves nothing about layout, CSS, or stacking. Combining into one
 * `.lli-root` is the structural proof that two status surfaces no longer
 * share those pixels. `$effect` rows/s is 0 on first SSR paint; do not
 * assert a rate.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const ENTRY = [
	"export { default as Indicator } from '$lib/components/rb/browser/LibraryLoadIndicator.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

let mod;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
});

function renderIndicator(props) {
	return mod.render(mod.Indicator, { props }).body;
}

function count(html, pattern) {
	return [...html.matchAll(pattern)].length;
}

// pins 1f9711b7, dd5fad7f, e452be6b: the strip root is reserved space, so it
// renders while idle too. What idle must NOT render is any status content.
test('idle props render the reserved strip with no status content', () => {
	const html = renderIndicator({ loading: false, progress: null, searching: false });
	assert.equal(count(html, /lli-root/g), 1, `rendered: ${html}`);
	assert.equal(html.includes('lli-mark'), false, `rendered: ${html}`);
	assert.equal(html.includes('lli-track'), false, `rendered: ${html}`);
	assert.equal(html.includes('searching whole collection'), false, `rendered: ${html}`);
	assert.equal(html.includes('loading...'), false, `rendered: ${html}`);
});

test('searching-only renders the search line in one status root, with no load track', () => {
	const html = renderIndicator({ loading: false, progress: null, searching: true });
	assert.match(html, /searching whole collection\.\.\./);
	assert.equal(count(html, /lli-root/g), 1, `rendered: ${html}`);
	assert.equal(count(html, /role="status"/g), 1, `rendered: ${html}`);
	assert.equal(html.includes('loading...'), false, 'searching-only must not invent a load label');
	assert.equal(html.includes('lli-track'), false, 'searching-only must not paint a load track');
});

test('loading-only renders the load label in one status root, with no search copy', () => {
	const html = renderIndicator({ loading: true, progress: null, searching: false });
	assert.match(html, /loading\.\.\./);
	assert.equal(count(html, /lli-root/g), 1, `rendered: ${html}`);
	assert.equal(html.includes('searching whole collection'), false, `rendered: ${html}`);
});

test('searching and loading stack both statuses in one root', () => {
	const html = renderIndicator({
		loading: true,
		progress: { loaded: 10, total: 100 },
		searching: true
	});
	assert.match(html, /searching whole collection\.\.\./);
	assert.match(html, /loading\.\.\./);
	assert.equal(count(html, /lli-root/g), 1, `rendered: ${html}`);
	assert.equal(count(html, /role="status"/g), 1, `rendered: ${html}`);
});
