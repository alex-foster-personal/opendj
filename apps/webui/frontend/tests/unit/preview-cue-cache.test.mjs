/**
 * Requirements: CUEOUT-15.
 *
 * The preview holds decoded PCM whose byte cost grows with duration.
 * The first revision kept one decoded buffer and released it only on an explicit stop or on previewing a different
 * track, which meant a long track previewed once stayed resident for the rest
 * of the session.
 *
 * Regression lines:
 * - if the budget is counted in TRACKS rather than bytes then three long
 *   tracks cost four times what three short ones do and the cap means nothing
 * - if the sounding track can be evicted then the operator hears a preview cut
 *   out because something else was clicked
 * - if a track larger than the whole budget is dropped on sight then it can
 *   never be previewed at all
 * - if eviction runs while the total already fits then a two-track cache is
 *   pointlessly emptied and every revisit pays a full decode again
 * - if the order is most-recently-used first then the cache evicts exactly the
 *   track the operator is flicking back to
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let previewCacheEvictions;

const MB = 1024 * 1024;
/** Legacy test sizing approximation; not a public corpus measurement. */
const track = (stable_id, seconds) => ({ stable_id, bytes: Math.round(seconds * 0.34 * MB) });

before(async () => {
	const mod = await loadTypeScriptModule('src/lib/player/preview-cue-cache.ts');
	previewCacheEvictions = mod.previewCacheEvictions;
});

describe('preview cache eviction', () => {
	it('drops nothing while the total fits', () => {
		const entries = [track('a', 176), track('b', 200)];
		assert.deepEqual(previewCacheEvictions(entries, 256 * MB, 'b'), []);
	});

	it('drops from the least-recently-used end until it fits', () => {
		// 60 + 68 + 77 + 130 = 335 MB against a 256 MB budget. Dropping the
		// oldest leaves 275, still over, so the next one goes too and the two
		// most recent survive.
		const entries = [track('a', 176), track('b', 200), track('c', 226), track('d', 383)];
		assert.deepEqual(previewCacheEvictions(entries, 256 * MB, 'd'), ['a', 'b']);
	});

	it('keeps dropping when one eviction is not enough', () => {
		const entries = [track('a', 383), track('b', 383), track('c', 383)];
		assert.deepEqual(previewCacheEvictions(entries, 256 * MB, 'c'), ['a', 'b']);
	});

	it('never evicts the track that is sounding, even at the LRU end', () => {
		const entries = [track('a', 383), track('b', 383), track('c', 383)];
		const drop = previewCacheEvictions(entries, 256 * MB, 'a');
		assert.equal(drop.includes('a'), false, 'the sounding track must survive');
		assert.deepEqual(drop, ['b', 'c']);
	});

	it('keeps a sounding track that is larger than the whole budget', () => {
		// There is no eviction that helps here, and dropping it is the one
		// eviction the operator could hear.
		assert.deepEqual(previewCacheEvictions([track('a', 900)], 256 * MB, 'a'), []);
	});

	it('an empty cache asks for nothing', () => {
		assert.deepEqual(previewCacheEvictions([], 256 * MB, null), []);
	});

	it('a byte budget, not a count: three short tracks survive where three long ones do not', () => {
		const short = [track('a', 60), track('b', 60), track('c', 60)];
		const long = [track('a', 383), track('b', 383), track('c', 383)];
		assert.deepEqual(previewCacheEvictions(short, 256 * MB, 'c'), []);
		assert.equal(previewCacheEvictions(long, 256 * MB, 'c').length, 2);
	});

	it('with nothing playing, everything is fair game', () => {
		const entries = [track('a', 383), track('b', 383), track('c', 383)];
		assert.deepEqual(previewCacheEvictions(entries, 0, null), ['a', 'b', 'c']);
	});
});
