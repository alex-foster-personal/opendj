/**
 * requirement: CHROME-07
 *
 * With MIDI on, access granted and no mapped controller connected, the mixer's
 * MIDI entries read red with a cross. There are two entries (the
 * headphone-cluster MIDI button and the MIDI entry inside the audio I/O menu),
 * and the requirement names both. The I/O-menu entry used to render the bare
 * word "MIDI" while the cluster button drew the glyph (Codex BLOCKING
 * 4133648249, PR #3896), so both now render one shared MidiStatusGlyph from
 * one midiLabelGlyph() derivation.
 *
 * The I/O menu lives in a ControlExplainer popover that only opens in the
 * browser, so SSR cannot mount it inside the cluster. The glyph component is
 * mounted through the real Svelte compiler here instead, and the cluster's
 * two buttons are checked to render that same component from the same value.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';
import { bundleSvelteEntry, renderToHtml } from './mount-svelte.mjs';

const CLUSTER_PATH = '../../src/lib/components/rb/mixer/HeadphoneCluster.svelte';
const cluster = readFileSync(fileURLToPath(new URL(CLUSTER_PATH, import.meta.url)), 'utf8');

let bundle;
before(async () => {
	bundle = await bundleSvelteEntry(`
		export { default as MidiStatusGlyph } from '$lib/components/rb/mixer/MidiStatusGlyph.svelte';
		export { midiLabelGlyph, midiLabelStatus } from '$lib/components/rb/midi/midi-format';
		export { MIDI_CROSS_PATH, MIDI_TICK_PATH } from '$lib/ui/icon-glyphs';
	`);
});

/** Each `<button ...>...</button>` in the cluster whose class list names midi-btn. */
function midiButtons(source) {
	return [...source.matchAll(/<button\b[\s\S]*?<\/button\s*>/g)]
		.map((m) => m[0])
		.filter((b) => /class="hp-btn midi-btn[^"]*"/.test(b));
}

test('MIDI on, granted, no mapped controller: the derivation says red with a cross', () => {
	const status = bundle.midiLabelStatus('granted', false, false, true);
	assert.equal(status, 'red');
	assert.equal(bundle.midiLabelGlyph(status), 'cross');
});

test('the shared glyph draws the cross for red and the tick for green, as an SVG', async () => {
	const cross = await renderToHtml(bundle.MidiStatusGlyph, { glyph: 'cross' });
	assert.match(cross, /<svg[^>]*data-midi-glyph="cross"/);
	assert.ok(cross.includes(`d="${bundle.MIDI_CROSS_PATH}"`), cross);
	const tick = await renderToHtml(bundle.MidiStatusGlyph, { glyph: 'tick' });
	assert.ok(tick.includes(`d="${bundle.MIDI_TICK_PATH}"`), tick);
	assert.notEqual(bundle.MIDI_CROSS_PATH, bundle.MIDI_TICK_PATH);
});

test('control: gray and amber draw no mark at all', async () => {
	for (const status of ['grey', 'amber']) {
		const html = await renderToHtml(bundle.MidiStatusGlyph, { glyph: bundle.midiLabelGlyph(status) });
		assert.doesNotMatch(html, /<svg/, `${status}: ${html}`);
	}
});

test('both mixer MIDI entries render the shared glyph from the one derived value', () => {
	const buttons = midiButtons(cluster);
	assert.equal(buttons.length, 2, 'expected the headphone-cluster and the I/O-menu MIDI entries');
	assert.ok(buttons.some((b) => b.includes('class="hp-btn midi-btn io-midi')), 'the I/O-menu entry is one of them');
	for (const button of buttons) {
		assert.match(button, /<MidiStatusGlyph glyph=\{midiGlyph\} \/>/, button);
	}
	assert.equal((cluster.match(/const midiGlyph = \$derived\(midiLabelGlyph\(midiStatus\)\)/g) ?? []).length, 1);
});

test('the glyph is drawn in one place, not copied into the cluster', () => {
	assert.doesNotMatch(cluster, /class="midi-glyph"/);
	assert.doesNotMatch(cluster, /M2 6\.2 5 9\.2 10 3|M3 3l6 6m0-6-6 6/);
});
