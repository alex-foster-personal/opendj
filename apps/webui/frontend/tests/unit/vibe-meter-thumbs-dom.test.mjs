/**
 * Pin 05a8586f4b43: thumbs mount with up/down roles and green/red CSS tokens (SSR).
 */
// requirement: UX-EXPLAIN-02
// [if] VibeMeter SSR mount [then] thumbs up/down buttons exist with green/red token wiring, else stop
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const VIBE_SRC = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/VibeMeter.svelte', import.meta.url)),
	'utf8'
);

const SSR_ENTRY = `
export { default as VibeMeter } from '$lib/components/rb/VibeMeter.svelte';
export { vibeState } from '$lib/rb/vibe.svelte';
`;

let ssr;

before(async () => {
	globalThis.window = globalThis.window ?? {
		localStorage: {
			getItem: () => null,
			setItem: () => {},
			removeItem: () => {}
		}
	};
	globalThis.fetch = async () => ({
		ok: true,
		json: async () => ({ items: [] })
	});
	ssr = await loadSvelteSsrModule(SSR_ENTRY);
	ssr.vibeState.display = 0.42;
	ssr.vibeState.peak = 0.5;
	ssr.vibeState.config_ready = false;
});

test('VibeMeter SSR renders thumbs up and down with distinct color tokens', async () => {
	const { render } = await import('svelte/server');
	const html = render(ssr.VibeMeter, {}).body;
	assert.match(html, /aria-label="thumbs up"/);
	assert.match(html, /aria-label="thumbs down"/);
	assert.match(html, /class="vibe-thumb up /);
	assert.match(html, /class="vibe-thumb down /);
	assert.match(VIBE_SRC, /--vibe-thumb-up:\s*#00c853/);
	assert.match(VIBE_SRC, /--vibe-thumb-down:/);
	const upBlock = VIBE_SRC.match(/\.vibe-thumb\.up\s*\{[\s\S]*?\}/)?.[0] ?? '';
	assert.doesNotMatch(upBlock, /--rb-yellow/);
});

function hexChannelDominance(hex) {
	const m = hex.match(/#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})/i);
	if (m === null) throw new Error(`expected #rrggbb, got ${hex}`);
	const r = Number.parseInt(m[1], 16);
	const g = Number.parseInt(m[2], 16);
	const b = Number.parseInt(m[3], 16);
	return { r, g, b };
}

test('VibeMeter thumb up/down CSS tokens resolve to green and red channels', () => {
	const upHex = VIBE_SRC.match(/--vibe-thumb-up:\s*(#[0-9a-fA-F]{6})/)?.[1];
	assert.ok(upHex, 'missing --vibe-thumb-up hex');
	const up = hexChannelDominance(upHex);
	assert.ok(up.g > up.r && up.g > up.b, `thumb up ${upHex} must be green-dominant`);
	const downBlock = VIBE_SRC.match(/\.vibe-thumb\.down\s*\{[\s\S]*?\}/)?.[0] ?? '';
	assert.match(downBlock, /--vibe-thumb-down/);
	assert.doesNotMatch(downBlock, /--rb-yellow|--rb-orange/);
});
