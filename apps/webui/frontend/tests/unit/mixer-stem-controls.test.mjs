/**
 * MIXUX-06 / #938: mixer stem controls are the deck's existing stem view,
 * rendered below the channel fader without creating a second state surface.
 *
 * Regression lines:
 * - if a ready stem bundle is loaded then the channel strip renders the shared StemRow
 * - if strip mute or solo is requested then it dispatches stem_mute or stem_solo on that deck
 * - if a bundle has no drums or no stems then StemRow keeps its existing inert reason
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

import { compile } from 'svelte/compiler';

async function source(relative) {
	return readFile(`src/lib/components/rb/${relative}`, 'utf8');
}

test('1e63026e90e8: channel strip compiles STEM label and shared StemRow below its fader', async () => {
	const strip = await source('mixer/ChannelStrip.svelte');
	assert.doesNotThrow(() => compile(strip, { filename: 'ChannelStrip.svelte', generate: 'server' }));
	assert.match(strip, /import StemRow from '\.\.\/deck\/StemRow\.svelte';/);
	assert.match(
		strip,
		/<div class="fader-slot">[\s\S]*?<\/div>\s*<button[\s\S]*class="stem-label"[\s\S]*>STEM<\/button[\s\S]*<div class="stem-slot">\s*<StemRow/,
		'StemRow must occupy the reserved STEM slot directly below the fader'
	);
	assert.match(strip, /onclick=\{\(\) => onStemEqMode/);
});

test('mixer routes strip gestures through the existing typed stem commands', async () => {
	const mixer = await source('Mixer.svelte');
	assert.match(mixer, /type: 'stem_mute'/);
	assert.match(mixer, /type: 'stem_solo'/);
	assert.match(mixer, /\{ type: 'stem_mute', deck, stem, muted: !muted \}/);
	assert.match(mixer, /\{ type: 'stem_solo', deck, stem, solo: !solo \}/);
	assert.match(mixer, /onStemMute=\{\(stem\) => handleStemMute\(deck, stem\)\}/);
	assert.match(mixer, /onStemSolo=\{\(stem\) => handleStemSolo\(deck, stem\)\}/);
});

test('the shared row remains the sole availability and RoFormer drums-reason implementation', async () => {
	const row = await source('deck/StemRow.svelte');
	assert.match(row, /disabled=\{!ready \|\| unavailable\(stem\.id\)\}/);
	assert.match(row, /aria-busy=\{pending\}/);
	assert.match(row, /it is mixed into INST, so it cannot be muted on its own/);
	assert.match(row, /stems \$\{deck\.stems\.status\}: \$\{deck\.stems\.error \?\? 'no aligned artifact'\}/);
});

test('deck and mixer scopes publish distinct stem chip testids', async () => {
	const row = await source('deck/StemRow.svelte');
	const strip = await source('mixer/ChannelStrip.svelte');
	assert.match(row, /testIdScope = 'deck'/);
	assert.match(row, /data-testid=\{`stem-\$\{stem\.id\}-\$\{testIdScope\}-\$\{deck\.deck_id\}`\}/);
	assert.match(strip, /testIdScope="channel"/);
});
