import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { compile } from 'svelte/compiler';

const filename = new URL('../../src/lib/components/rb/Deck.svelte', import.meta.url);
const source = readFileSync(filename, 'utf8');
const theme = readFileSync(new URL('../../src/lib/rb/theme.css', import.meta.url), 'utf8');

test('deck warning uses the rendered jog warning instead of independent sync thresholds', () => {
	assert.match(theme, /\.perf-root \.rb-deck:has\(\.jog-off-tempo\)::before/);
	compile(source, { filename: filename.pathname, generate: 'client' });
});

test('deck warning is non-interactive and respects reduced motion', () => {
	const warning = theme.slice(theme.indexOf('/* Deck warning chrome'), theme.indexOf('/* -------- knob:'));
	assert.match(warning, /pointer-events: none/);
	assert.match(warning, /animation: deck-tempo-warning/);
	assert.match(warning, /prefers-reduced-motion: reduce/);
	assert.match(warning, /animation: none/);
});
