/**
 * Issue #11466: /performance first-paint bundle deferrals.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const read = (rel) => readFileSync(fileURLToPath(new URL(`../../${rel}`, import.meta.url)), 'utf8');

const staticImportOf = (file) => new RegExp(`import\\s+\\w+\\s+from\\s+['"][^'"]*${file}['"]`);
const sideEffectStage = /import\s+['"]\$lib\/components\/lyrics\/StageOverlay\.svelte['"]/;
const dynamicImportOf = (file) => new RegExp(`import\\(\\s*['"][^'"]*${file}['"]\\s*\\)`);

test('[if] /performance +page is parsed [then] it does not static-import StageOverlay, CommandBar, or cmdk v2', () => {
	const page = read('src/routes/performance/+page.svelte');
	assert.doesNotMatch(page, sideEffectStage);
	assert.doesNotMatch(page, staticImportOf('CommandBar\\.svelte'));
	assert.doesNotMatch(page, staticImportOf('CommandBarV2\\.svelte'));
	assert.match(page, dynamicImportOf('CommandBar\\.svelte'));
	// CMDK-9467 moved the v2 bar to the root layout's host, which is itself a lazy chunk.
	const layout = read('src/routes/+layout.svelte');
	assert.doesNotMatch(layout, staticImportOf('CommandPaletteHost\\.svelte'));
	assert.doesNotMatch(layout, staticImportOf('CommandBarV2\\.svelte'));
	assert.match(layout, dynamicImportOf('CommandPaletteHost\\.svelte'));
	assert.match(page, /prefetchStageOverlay\(_reportStageOverlayLoadFailure\)/);
});

test('[if] browser-panel-support is parsed [then] filter popovers load only through import()', () => {
	const support = read('src/lib/components/rb/browser/browser-panel-support.ts');
	assert.doesNotMatch(support, staticImportOf('CompatibleFilterPopover\\.svelte'));
	assert.doesNotMatch(support, staticImportOf('LibraryKeyFilterPopover\\.svelte'));
	assert.match(support, /loadCompatibleFilterPopover/);
	assert.match(support, /loadLibraryKeyFilterPopover/);
});

test('[if] BrowserPanel is parsed [then] filter popovers defer until bootScheduler runs', () => {
	const panel = read('src/lib/components/rb/BrowserPanel.svelte');
	assert.match(panel, /bootScheduler\.defer\('browser:filter-popovers'/);
	assert.match(panel, /loadCompatibleFilterPopover\(\)/);
	assert.match(panel, /loadLibraryKeyFilterPopover\(\)/);
	assert.match(panel, /\{#if LibraryKeyFilterPopover\}/);
	assert.match(panel, /\{#if CompatibleFilterPopover\}/);
});

test('[if] preload1 +page is parsed [then] it imports preset only via import()', () => {
	const page = read('src/routes/performance/preload1/+page.svelte');
	assert.doesNotMatch(page, /from '\.\/preset'/);
	assert.match(page, /await import\('\.\/preset'\)/);
});
