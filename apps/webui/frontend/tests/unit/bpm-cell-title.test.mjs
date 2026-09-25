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
});
