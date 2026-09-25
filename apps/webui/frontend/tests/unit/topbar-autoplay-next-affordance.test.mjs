/**
 * PLAY-12 / issue #3884: TopBar next-track affordance shape guard.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

test('SHAPE GUARD: ap-next-btn highlights when AutoPlay is enabled and hover turns accent', () => {
	const topbar = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/TopBar.svelte', import.meta.url)),
		'utf8'
	);
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
	assert.match(
		topbar,
		/class:on=\{uiPrefs\.auto_play_enabled \|\| autoPlayNextState\.armed\}/,
		'PLAY-13: ap-wrap shares the active border with AutoPlay and next'
	);
	assert.match(topbar, /\.ap-wrap\.on\s*\{[^}]*border:\s*1px solid var\(--rb-accent\)/);
});
