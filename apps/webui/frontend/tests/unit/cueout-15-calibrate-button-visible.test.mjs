// requirement: CUEOUT-15 / IOPIN-07 (CALIBRATE is discoverable with device pickers)
// [if] the mixer is in two_outputs mode [then] the CALIBRATE button is in the device menu, disabled until cue is picked
// [if] CALIBRATE sits outside the device pickers [then] operators lose context during calibration - broken
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const SOURCE = new URL(
	'../../src/lib/components/rb/mixer/HeadphoneCluster.svelte',
	import.meta.url
);

test('CALIBRATE stays inside I/O beside the output pickers when two_outputs is active', () => {
	const source = readFileSync(SOURCE, 'utf8');
	assert.match(source, /\{#if ioSurface\.open\}[\s\S]*?class="hp-panel"/);
	assert.match(source, /data-io-calibration-block/);
	assert.match(source, /aria-label="CALIBRATE CUE ALIGNMENT"/);
	assert.match(
		source,
		/disabled=\{!calibrateEnabled\}/,
		'it stays disabled until a cue device is selected'
	);
});
