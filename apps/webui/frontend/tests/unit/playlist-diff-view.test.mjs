import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let hasComputedDiff;

before(async () => {
	const mod = await loadTypeScriptModule('src/lib/playlist-diff-view.ts');
	hasComputedDiff = mod.hasComputedDiff;
});

const EMPTY_DIFF = { rb_only: [], djay_only: [], both: [], conflicts: [] };

describe('hasComputedDiff', () => {
	it('is false for the all-empty PlaylistDiff() the route serves when no diff was computed', () => {
		assert.equal(hasComputedDiff(EMPTY_DIFF), false);
	});

	it('is true when rb_only carries an entry', () => {
		assert.equal(hasComputedDiff({ ...EMPTY_DIFF, rb_only: ['stable-aaa'] }), true);
	});

	it('is true when djay_only carries an entry', () => {
		assert.equal(hasComputedDiff({ ...EMPTY_DIFF, djay_only: ['stable-ccc'] }), true);
	});

	it('is true when both carries an entry', () => {
		assert.equal(hasComputedDiff({ ...EMPTY_DIFF, both: ['stable-ddd'] }), true);
	});

	it('is true when only conflicts carries an entry, with rb_only/both/djay_only empty', () => {
		assert.equal(
			hasComputedDiff({
				...EMPTY_DIFF,
				conflicts: [{ stable_id: 'stable-ddd', rb_position: 5, djay_position: 7 }]
			}),
			true
		);
	});
});
