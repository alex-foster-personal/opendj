/**
 * TRANS-01: TopBar pill sits immediately right of the vibe meter.
 *
 * [if] decks 1 and 2 are both active and the default mixer is at xfader 0.5
 *   with faders at 1 [then] the TopBar pill shows transitioning
 * Detection itself is the classifier / query live tests. This file pins
 * placement, markup, idle hide, and the no-device light tooltip.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const CHIP = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/TransitioningChip.svelte', import.meta.url)),
	'utf8'
);
const TOPBAR = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/TopBar.svelte', import.meta.url)),
	'utf8'
);
const LIGHT = readFileSync(
	fileURLToPath(new URL('../../src/lib/rb/transition-status-light.ts', import.meta.url)),
	'utf8'
);

test('TopBar mounts TransitioningChip immediately after the vibe slot', () => {
	const chipAt = TOPBAR.indexOf('<TransitioningChip />');
	const vibeSlotAt = TOPBAR.indexOf('vibe-slot');
	const vibeMeterAt = TOPBAR.indexOf('<VibeMeter />');
	const pairingAt = TOPBAR.indexOf('topbar-slot-pairing');
	const pairingLabelAt = TOPBAR.indexOf('Create pairing');
	assert.ok(chipAt >= 0, 'TopBar must render TransitioningChip');
	assert.ok(vibeSlotAt >= 0 && vibeMeterAt >= 0, 'TopBar must keep the vibe slot');
	assert.ok(chipAt > vibeSlotAt, 'chip must sit after vibe-slot');
	assert.ok(chipAt > vibeMeterAt, 'chip must sit after VibeMeter');
	assert.ok(chipAt < pairingAt, 'chip must sit before the Create pairing cluster');
	assert.ok(chipAt < pairingLabelAt, 'chip must sit before the Create pairing label');
});

test('chip source is a live status pill that hides while idle', () => {
	assert.match(CHIP, /role="status"/);
	assert.match(CHIP, /aria-live="polite"/);
	assert.match(CHIP, /pointer-events:\s*none/);
	assert.match(CHIP, /data-testid="transition-chip"/);
	assert.match(CHIP, /•/);
	assert.match(CHIP, /transitioning/);
	assert.match(CHIP, /approaching/);
	assert.match(CHIP, /\{#if status\.state !== 'idle'\}/);
	assert.doesNotMatch(CHIP, /<button/);
	assert.doesNotMatch(CHIP, /tabindex/);
});

test('light tooltip comes from the adapter describe string', () => {
	assert.match(CHIP, /getTransitionStatusLight/);
	assert.match(LIGHT, /no device/i);
	assert.match(LIGHT, /not implemented/i);
	assert.match(LIGHT, /export interface TransitionStatusLight/);
});

test('default engine state SSR renders no transitioning text', async () => {
	const entry = [
		"export { default as Chip } from '$lib/components/rb/TransitioningChip.svelte';",
		"export { render } from 'svelte/server';"
	].join('\n');
	const ssr = await loadSvelteSsrModule(entry);
	const body = ssr.render(ssr.Chip).body;
	assert.doesNotMatch(body, /transitioning/);
	assert.doesNotMatch(body, /approaching/);
});
