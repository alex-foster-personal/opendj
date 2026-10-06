import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const FRONTEND_ROOT = join(dirname(fileURLToPath(import.meta.url)), '../..');
const PANE_TABS = readFileSync(
	join(FRONTEND_ROOT, 'src/lib/components/rb/browser/PaneTabs.svelte'),
	'utf8'
);
const BROWSER_PANEL = readFileSync(
	join(FRONTEND_ROOT, 'src/lib/components/rb/BrowserPanel.svelte'),
	'utf8'
);

test('active ephemeral blank list tab omits the vertical stepper column', () => {
	assert.match(PANE_TABS, /\{#if tab\.ephemeral\}[\s\S]*save as[\s\S]*\{:else\}[\s\S]*class="stepper"/);
	assert.match(PANE_TABS, /\.tab\.active[\s\S]*max-height:\s*24px/);
});

// REQ: LIBUX-19, LIBUX-49
test('browser header keeps its edit actions behind the one pencil menu', () => {
	assert.match(BROWSER_PANEL, /<LibraryEditMenu hasSelection=\{pane\.selected_ids\.length > 0\}/);
	assert.doesNotMatch(BROWSER_PANEL, /edit-actions-(stack|fold)/);
	assert.match(BROWSER_PANEL, /class="header-controls-cluster"/);
});

// REQ: LIBUX-19
test('browser boots with one pane slot and Blank List (+) affordance', () => {
	assert.match(BROWSER_PANEL, /const panes: PaneStore\[\] = \[createPaneStore\(\)\]/);
	assert.match(PANE_TABS, /Blank List \(\+\)/);
});
