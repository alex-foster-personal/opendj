import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
	proposalConfidenceLabel,
	proposalConfidenceReadout,
	proposalConfidenceTitle
} from '../../src/lib/autodj/propose-confidence.ts';

describe('proposal confidence readout', () => {
	it('formats label and four-factor title for hover', () => {
		assert.equal(
			proposalConfidenceLabel(0.62, 'structure'),
			'confidence 0.62 (weakest: structure)'
		);
		assert.equal(
			proposalConfidenceTitle({ grid: 1, structure: 0.7, vocal: 1, gap: 0.88 }),
			'grid 1.00; structure 0.70; vocal 1.00; gap 0.88'
		);
		const out = proposalConfidenceReadout(0.62, 'structure', {
			grid: 1,
			structure: 0.7,
			vocal: 1,
			gap: 0.88
		});
		assert.equal(out.label, 'confidence 0.62 (weakest: structure)');
		assert.match(out.title, /structure 0\.70/);
	});
});
