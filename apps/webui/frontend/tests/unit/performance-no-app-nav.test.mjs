// requirement: PERF-UI-07 (supersedes PERF-UI-02 and PERF-UI-04)
// [if] /performance is showing [then] no fixed bottom-left link strip is
// mounted, and every page it used to link is still reachable: Library through
// the top bar's mode picker, the rest through the app-shell sidebar.
//
// Regression lines:
// - if the root layout mounts a nav beside the performance route then the
//   removed link strip is back
// - if the mode picker loses its Library card, or stops marking the library
//   mode exit, then /performance has no way out without a typed URL
// - if the app-shell sidebar drops a link the strip used to carry then that
//   page is reachable only by typing its address

import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function sourcePath(relative) {
	return fileURLToPath(new URL(`../../src/${relative}`, import.meta.url));
}

function source(relative) {
	return readFileSync(sourcePath(relative), 'utf8');
}

const LAYOUT = source('routes/+layout.svelte');
const TOP_BAR = source('lib/components/rb/TopBar.svelte');
const APP_MODE = source('lib/rb/app-mode.ts');

/** Every page the removed strip linked to, by route. */
const FORMER_STRIP_ROUTES = ['/', '/reconcile', '/dedup', '/smartlists', '/admin'];

test('the link strip component is gone and the root layout mounts nothing in its place', () => {
	assert.equal(existsSync(sourcePath('lib/components/PerformanceAppNav.svelte')), false);
	assert.doesNotMatch(LAYOUT, /PerformanceAppNav/);
	assert.doesNotMatch(LAYOUT, /performance-app-nav/);
	// Positive half: the full-bleed branch this test reads really is the one
	// the performance route renders through, so "no nav in it" is a statement
	// about the right block and not about a string that moved.
	const start = LAYOUT.indexOf('{#if isFullBleedRoute}');
	const end = LAYOUT.indexOf('<div class="app-shell">');
	assert.ok(start !== -1 && end > start, 'full-bleed branch must precede the app shell');
	const fullBleedBranch = LAYOUT.slice(start, end);
	assert.match(fullBleedBranch, /\{@render children\(\)\}/);
	assert.doesNotMatch(fullBleedBranch, /<nav\b/);
	assert.doesNotMatch(fullBleedBranch, /<a\b/);
});

test('Library stays reachable from /performance through the mode picker', () => {
	assert.match(APP_MODE, /id:\s*'library',\s*\n\s*label:\s*'Library',\s*\n\s*href:\s*'\/'/);
	assert.match(TOP_BAR, /\{#each APP_MODES as mode \(mode\.id\)\}/);
	assert.match(TOP_BAR, /href=\{mode\.href\}/);
	// The strip's Library link marked the library-mode exit before navigating;
	// the mode picker is now the only door, so it has to keep doing that.
	assert.match(TOP_BAR, /if \(modeId === 'library'\) markLibraryModeExit\(\);/);
});

test('the app-shell sidebar still links every page the strip used to carry, and the ledger', () => {
	const sidebarStart = LAYOUT.indexOf('<aside class="sidebar">');
	const sidebarEnd = LAYOUT.indexOf('</aside>');
	assert.ok(sidebarStart !== -1 && sidebarEnd > sidebarStart, 'sidebar block must exist');
	const sidebar = LAYOUT.slice(sidebarStart, sidebarEnd);
	for (const route of FORMER_STRIP_ROUTES) {
		assert.ok(sidebar.includes(`<a href="${route}">`), `sidebar must link ${route}`);
	}
	assert.match(sidebar, /href="\/progress-tree"/);
	// Negative control: the same probe reports a route the sidebar never had.
	assert.equal(sidebar.includes('<a href="/no-such-route">'), false);
});

test('each former strip destination still has a route file', () => {
	for (const route of FORMER_STRIP_ROUTES) {
		const file = `routes${route === '/' ? '' : route}/+page.svelte`;
		assert.equal(existsSync(sourcePath(file)), true, `${file} must exist`);
	}
	assert.equal(existsSync(sourcePath('routes/progress-tree/+page.svelte')), true);
});
