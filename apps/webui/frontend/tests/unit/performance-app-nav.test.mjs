// requirement: PERF-UI-02
// [if] /performance is showing [then] Library and Admin links are mounted
// from the root layout and are not hidden by a max-width breakpoint

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function source(relative) {
	return readFileSync(fileURLToPath(new URL(`../../src/${relative}`, import.meta.url)), 'utf8');
}

const LAYOUT = source('routes/+layout.svelte');
const NAV = source('lib/components/PerformanceAppNav.svelte');
const TOP_BAR = source('lib/components/rb/TopBar.svelte');
const BROWSER_PANEL = source('lib/components/rb/BrowserPanel.svelte');
const APP_MODE = source('lib/rb/app-mode.ts');

test('the root layout mounts PerformanceAppNav only on performance routes', () => {
	assert.match(LAYOUT, /import PerformanceAppNav from '\$lib\/components\/PerformanceAppNav\.svelte'/);
	const perfBranch = LAYOUT.slice(LAYOUT.indexOf('{#if isPerformance}'));
	assert.match(perfBranch, /\{@render children\(\)\}\s*\n\s*<PerformanceAppNav \/>/);
	const shellBranch = LAYOUT.slice(LAYOUT.indexOf('<div class="app-shell">'));
	assert.doesNotMatch(shellBranch, /PerformanceAppNav/);
});

test('PerformanceAppNav marks library mode exit before navigating to Library', () => {
	assert.match(NAV, /markLibraryModeExit/);
	assert.match(NAV, /performance-nav-library/);
});

test('PerformanceAppNav exposes Library and Admin links with the locked contract', () => {
	assert.match(NAV, /href="\/"/);
	assert.match(NAV, /href="\/admin"/);
	assert.match(NAV, /data-testid="performance-app-nav"/);
	assert.match(NAV, /aria-label="App navigation"/);
	assert.match(NAV, /position:\s*fixed/);
	assert.match(NAV, /z-index:\s*50/);
	assert.doesNotMatch(NAV, /@media/);
	assert.doesNotMatch(NAV, /display:\s*none/);
});

test('the hatch lives outside rb/ clone chrome and does not touch TopBar or BrowserPanel', () => {
	assert.doesNotMatch(NAV, /components\/rb\/PerformanceAppNav/);
	assert.doesNotMatch(TOP_BAR, /performance-app-nav/);
	assert.doesNotMatch(BROWSER_PANEL, /performance-app-nav/);
});

test('library-management Prep mode is selectable on the chooser', () => {
	assert.match(APP_MODE, /id:\s*'library-management'/);
	assert.match(APP_MODE, /label:\s*'Prep'/);
	assert.match(APP_MODE, /available:\s*true/);
});
