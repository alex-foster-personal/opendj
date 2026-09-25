import assert from 'node:assert/strict';
import { test } from 'node:test';

const { parseWaveformDesign, WAVEFORM_DESIGN_DEFAULT } = await import(
	'../../src/lib/rb/waveform-design.ts'
);

test('waveform design pref defaults and validates', () => {
	assert.equal(WAVEFORM_DESIGN_DEFAULT, 'tri-band');
	assert.equal(parseWaveformDesign('line'), 'line');
	assert.throws(() => parseWaveformDesign('stem'));
});
