/**
 * MIXUX-06 / #938: mixer stem controls are the deck's existing stem view,
 * rendered below the channel fader without creating a second state surface.
 *
 * Regression lines:
 * - if a ready stem bundle is loaded then the channel strip renders the shared StemRow
 * - if strip mute or solo is requested then it dispatches stem_mute or stem_solo on that deck
 * - if a bundle has no drums or no stems then StemRow keeps its existing inert reason
 * - if StemRow drops wheel/dblclick/level bar or ChannelStrip drops onGain then STEMPERF-11699 wiring broke
 * - if StemRow.svelte defers stem mute (setTimeout or STEM_CHIP_DBLCLICK_MS) then Class A regressed (PRRT_kwDOSEvNd861jTeZ) - broken
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
	assert.doesNotThrow(() =>
		compile(strip, { filename: 'ChannelStrip.svelte', generate: 'server' })
	);
	assert.match(strip, /import StemRow from '\.\.\/deck\/StemRow\.svelte';/);
	assert.match(
		strip,
		/\{#snippet faderBlock\(\)\}[\s\S]*?class="fader-slot"[\s\S]*?\{\/snippet\}/,
		'the fader-slot lives in the faderBlock snippet (MIXUX-9832 per-mode DOM order)'
	);
	assert.match(
		strip,
		/<span class="stem-label"[\s\S]*?STEM[\s\S]*?<\/span\s*>\s*<div class="stem-slot">\s*<StemRow/,
		'StemRow must occupy the reserved STEM slot directly under its STEM heading'
	);
	assert.match(
		strip,
		/\{#if !less\}\{@render cueBlock\(\)\}\{@render faderBlock\(\)\}\{\/if\}\s*<span class="stem-label"/,
		'MORE mode keeps CUE and fader above the STEM block (flex column visual order)'
	);
	// MIXUX-8244: the heading is a label now; the S button in the mixer shows stem dials.
	assert.doesNotMatch(strip, /onStemEqMode/);
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
	// STEM-48: the reason a chip is not live comes from the one shared mapping,
	// for the deck and the mixer strip alike (tests/unit/stem-status-view.test.mjs).
	assert.match(row, /const view = \$derived\(stemStatusView\(deck\.stems\)\);/);
	assert.match(row, /const statusTip: string = \$derived\(view\.tip\);/);
});

test('deck and mixer scopes publish distinct stem chip testids', async () => {
	const row = await source('deck/StemRow.svelte');
	const strip = await source('mixer/ChannelStrip.svelte');
	assert.match(row, /testIdScope = 'deck'/);
	assert.match(row, /data-testid=\{`stem-\$\{stem\.id\}-\$\{testIdScope\}-\$\{deck\.deck_id\}`\}/);
	assert.match(strip, /testIdScope="channel"/);
});

test('STEMPERF-11699: stem chips wire scroll-to-level, double-click, level bar and onGain (#10323)', async () => {
	const row = await source('deck/StemRow.svelte');
	for (const fragment of [
		'use:wheelAdjust={stemChipWheelParams(',
		'ondblclick=',
		'class="level"',
		'onGain'
	]) {
		assert.ok(row.includes(fragment), `StemRow.svelte lost ${fragment}`);
	}
	assert.ok(
		!row.includes('setTimeout'),
		'StemRow.svelte defers stem mute (Class A, PRRT_kwDOSEvNd861jTeZ)'
	);
	assert.ok(
		!row.includes('STEM_CHIP_DBLCLICK_MS'),
		'StemRow.svelte defers stem mute (Class A, PRRT_kwDOSEvNd861jTeZ)'
	);
	const strip = await source('mixer/ChannelStrip.svelte');
	assert.match(strip, /onGain=\{onStemGain\}/);
});
