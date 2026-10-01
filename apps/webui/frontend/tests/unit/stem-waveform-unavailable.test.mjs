import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let ui;

before(async () => {
	ui = await loadTypeScriptModule('src/lib/components/rb/wave/stem-waveform-ui.ts');
});

test('roformer2 drums row is inert with an explicit layout reason', () => {
	const deck = {
		stable_id: 'abc',
		stems: {
			status: 'ready',
			layout: 'roformer2',
			available_controls: ['vocal', 'instrumental'],
			error: null
		}
	};
	const tip = ui.stemWaveRowUnavailableTip(deck, 'drums');
	assert.ok(tip !== null);
	assert.match(tip, /not a separate stem/i);
	assert.match(tip, /roformer2/i);
});

test('ready demucs4 drums row is available', () => {
	const deck = {
		stable_id: 'abc',
		stems: {
			status: 'ready',
			layout: 'demucs4',
			available_controls: ['vocal', 'instrumental', 'drums'],
			error: null
		}
	};
	assert.equal(ui.stemWaveRowUnavailableTip(deck, 'drums'), null);
});

// [if] a demucs4 deck shows BASS / HARM rows [then] each asks the waveform
// route for its own bundle part, [else] the row is a blank canvas that is
// neither drawn nor marked unavailable.
test('every available demucs4 control maps to the waveform part the route serves', () => {
	assert.equal(ui.stemWaveformApiPart('vocal', 'demucs4'), 'vocals');
	assert.equal(ui.stemWaveformApiPart('drums', 'demucs4'), 'drums');
	assert.equal(ui.stemWaveformApiPart('bass', 'demucs4'), 'bass');
	assert.equal(ui.stemWaveformApiPart('other', 'demucs4'), 'other');
	assert.equal(ui.stemWaveformApiPart('instrumental', 'demucs4'), 'instrumental');
});

test('a roformer2 bundle has no drums, bass or other part to ask for', () => {
	assert.equal(ui.stemWaveformApiPart('vocal', 'roformer2'), 'vocals');
	assert.equal(ui.stemWaveformApiPart('instrumental', 'roformer2'), 'instrumental');
	for (const control of ['drums', 'bass', 'other']) {
		assert.equal(ui.stemWaveformApiPart(control, 'roformer2'), null, control);
		assert.equal(ui.stemWaveformApiPart(control, null), null, control);
	}
});
