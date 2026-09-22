// requirement: CUEOUT-15 (CALIBRATE is discoverable before a cue device is picked)
// [if] the mixer is in practice mode [then] the CALIBRATE button is rendered, disabled, so the operator can find it
// [if] the button lives inside the two_outputs-only block [then] it is invisible until a CUE device is selected - broken
// (found Wed 16 Sep 2026 driving the preview: the I/O row showed no CALIBRATE at all in practice mode)
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const SOURCE = new URL('../../src/lib/components/rb/mixer/HeadphoneCluster.svelte', import.meta.url);

test('CALIBRATE stays inside I/O but outside the two_outputs-only section', () => {
	const source = readFileSync(SOURCE, 'utf8');
	assert.match(source, /\{#if ioOpen\}[\s\S]*?class="hp-panel"/);
	assert.match(source, /\{#if headphoneState\.output_mode === 'two_outputs'\}[\s\S]*?<\/section>\s*\{\/if\}\s*<section class="hp-section hp-calibration-section"/,
		'CALIBRATE must be rendered after the two_outputs-only section, still within I/O');
	assert.match(source, /aria-label="CALIBRATE CUE ALIGNMENT"/);
	assert.match(source, /disabled=\{!calibrateEnabled\}/, 'it stays disabled until a cue device is selected');
});
