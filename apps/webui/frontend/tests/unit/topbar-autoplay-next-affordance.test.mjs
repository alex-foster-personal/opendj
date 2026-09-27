/**
 * PLAY-12 / issue #3884: TopBar next-track affordance shape guard.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

test('fc60002b81a8: ap-next-btn split shares AutoPlay highlight, hover accent, and >| affordance', () => {
	const topbar = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/TopBar.svelte', import.meta.url)),
		'utf8'
	);
	assert.match(topbar, /class="bsm-toggle ap-next-btn"/);
	assert.match(topbar, /&gt;\|/);
	assert.match(
		topbar,
		/class:on=\{uiPrefs\.auto_play_enabled \|\| autoPlayNextState\.armed\}/,
		'ap-next-btn must share the AutoPlay active highlight'
	);
	assert.match(
		topbar,
		/\.ap-wrap > \.ap-next-btn:hover\s*\{[^}]*color:\s*var\(--rb-accent\)/,
		'ap-next-btn hover must turn the icon accent-blue'
	);
	assert.match(topbar, /Next-track loop/);
});
