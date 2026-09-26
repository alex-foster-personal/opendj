import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

test('TrackTable draws full-width row separators on tbody tr', async () => {
	const src = await readFile('src/lib/components/rb/browser/TrackTable.svelte', 'utf8');
	assert.match(src, /tbody tr::after/);
	assert.match(src, /tbody td[\s\S]*border-bottom: none/);
});

/** The body of the FIRST rule whose selector is exactly `selector`. */
function ruleBody(src, selector) {
	const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
	const match = src.match(new RegExp(`\\n\\t${escaped} \\{([^}]*)\\}`));
	assert.ok(match, `no \`${selector} {}\` rule in TrackTable.svelte`);
	return match[1];
}

test('if the row separator loses its z-index then artwork and cell content paint over it (LIBUX-20) - broken', async () => {
	const src = await readFile('src/lib/components/rb/browser/TrackTable.svelte', 'utf8');
	const separator = ruleBody(src, 'tbody tr::after');
	const z = separator.match(/z-index:\s*(-?\d+)/);
	assert.ok(z, 'tbody tr::after must carry an explicit z-index');
	assert.ok(Number(z[1]) >= 1, `tbody tr::after z-index ${z[1]} must sit above z-index:auto cells`);
});

test('if a body cell sets a z-index then its fixed popovers are trapped under later rows - broken', async () => {
	const src = await readFile('src/lib/components/rb/browser/TrackTable.svelte', 'utf8');
	const cell = ruleBody(src, 'tbody td');
	assert.match(cell, /position:\s*relative/);
	assert.doesNotMatch(cell, /z-index/, 'tbody td must not create a stacking context');
});
