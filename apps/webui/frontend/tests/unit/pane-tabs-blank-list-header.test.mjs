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

test('browser header groups edit actions under Find and Replace', () => {
	assert.match(BROWSER_PANEL, /class="edit-actions-stack"/);
	assert.match(BROWSER_PANEL, /class="edit-actions-fold"/);
	assert.match(BROWSER_PANEL, /class="header-controls-cluster"/);
});

test('browser boots with one pane slot and Blank List (+) affordance', () => {
	assert.match(BROWSER_PANEL, /const panes: PaneStore\[\] = \[createPaneStore\(\)\]/);
	assert.match(PANE_TABS, /Blank List \(\+\)/);
});
