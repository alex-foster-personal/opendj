/**
 * requirement: CHROME-07
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const panel = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)),
	'utf8'
);

test('bottom tray renders MIDI 20px left of Preview', () => {
	assert.match(panel, /class="tray-midi"/);
	assert.match(panel, /class="tray-preview"/);
	assert.match(panel, /margin-right:\s*20px/);
	const midiAt = panel.indexOf('class="tray-midi"');
	const previewAt = panel.indexOf('class="tray-preview"');
	assert.ok(midiAt >= 0 && previewAt > midiAt);
});

test('tray MIDI opens the I/O view', () => {
	assert.match(panel, /openIoView\(\)/);
});

test('openIoView pins the headphone I/O menu without auto-opening MIDI drawer', () => {
	const ioSurface = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/io-surface.svelte.ts', import.meta.url)),
		'utf8'
	);
	const cluster = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/mixer/HeadphoneCluster.svelte', import.meta.url)),
		'utf8'
	);
	assert.doesNotMatch(ioSurface, /toggleMidiPanel/);
	assert.match(cluster, /programmaticOpen=\{ioSurface\.open\}/);
	assert.match(cluster, /onProgrammaticClose=\{closeIoView\}/);
});
