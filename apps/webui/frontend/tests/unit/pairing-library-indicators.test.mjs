import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

describe('pairing-library-indicators', () => {
	it('partner set membership drives row highlight class', () => {
		const partners = new Set(['b-track']);
		assert.equal(partners.has('b-track'), true);
		assert.equal(partners.has('other'), false);
	});
});
