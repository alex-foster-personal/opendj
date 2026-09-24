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
