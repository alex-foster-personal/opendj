// requirement: CUEOUT-15 (CALIBRATE is discoverable before a cue device is picked)
// [if] the mixer is in practice mode [then] the CALIBRATE button is rendered, disabled, so the operator can find it
// [if] the button lives inside the two_outputs-only block [then] it is invisible until a CUE device is selected - broken
// (found Wed 16 Sep 2026 driving the preview: the I/O row showed no CALIBRATE at all in practice mode)
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const SOURCE = new URL('../../src/lib/components/rb/mixer/HeadphoneCluster.svelte', import.meta.url);

test('if CALIBRATE is only rendered in two_outputs then the operator cannot find it in practice mode', () => {
	const source = readFileSync(SOURCE, 'utf8');
	const twoOutputsBlock = source.indexOf("{#if state.output_mode === 'two_outputs'}");
	const calibrate = source.indexOf('aria-label="CALIBRATE CUE ALIGNMENT"');
	assert.ok(twoOutputsBlock > 0 && calibrate > 0, 'both markers must exist');
	assert.ok(calibrate < twoOutputsBlock, 'CALIBRATE must be rendered outside (before) the two_outputs-only block');
	assert.match(source, /disabled=\{!calibrateEnabled\}/, 'it stays disabled until a cue device is selected');
});
