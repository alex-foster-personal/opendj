import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

describe('bpm-cell-title', () => {
	it('includes beatgrid method and confidence when present', async () => {
		const { bpmCellTitle } = await loadTypeScriptModule('src/lib/rb/bpm-cell-title.ts');
		const title = bpmCellTitle(
			{
				bpm_status: 'ok',
				bpm: 128,
				bpm_method: 'Rekordbox beatgrid',
				bpm_confidence: 0.92,
				bpm_reason: null
			},
			128
		);
		assert.match(title, /Rekordbox beatgrid/);
		assert.match(title, /Confidence: 92%/);
	});

	it('names an invalid stored confidence instead of hiding it (Sol P1, PR #4014)', async () => {
		const { bpmCellTitle } = await loadTypeScriptModule('src/lib/rb/bpm-cell-title.ts');
		const error = 'invalid stored bpm confidence 1.5: expected a number from 0 to 1';
		const title = bpmCellTitle(
			{
				bpm_status: 'ok',
				bpm: 128,
				bpm_method: null,
				bpm_confidence: null,
				bpm_confidence_error: error,
				bpm_reason: null
			},
			null
		);
		assert.ok(title.includes(`Confidence: ${error}`), title);
		assert.doesNotMatch(title, /Dynamic tempo analysis: not analyzed/);
	});
});
