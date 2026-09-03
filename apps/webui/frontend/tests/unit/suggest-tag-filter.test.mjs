/**
 * Which rationale tags earn their line on a NEXT tile.
 *
 * Pin 407a1601defe (the maintainer, Wed 2 Sep 2026): "NEXT does not need these bpm match
 * / camelot step labesl - they're doing no work. Save the vertical space."
 *
 * He is right, and the reason is worth writing down: the tile's meta row
 * already prints the BPM, the key and the energy, and the tile is only IN the
 * NEXT list because those matched. So "bpm match" under a tile that shows the
 * BPM next to the master's tells the reader nothing they cannot see, while
 * costing a whole line on a strip where vertical space is the scarce thing.
 *
 * `pair_*` is the exception and must survive: "this came from a pairing you
 * made" is not visible anywhere else on the tile, so it is the one tag that is
 * carrying information rather than restating the row above it.
 *
 * Regression lines:
 * - if a bpm/camelot/energy tag renders again then the strip is paying a line
 *   for something the meta row already says
 * - if a pair tag stops rendering then the only tag that carries new
 *   information has been dropped with the redundant ones
 * - if an unknown future tag is silently dropped then a new rationale the
 *   engine starts emitting never reaches the screen
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let visibleRationaleTags;

before(async () => {
	({ visibleRationaleTags } = await loadTypeScriptModule('src/lib/rb/suggest-tags.ts'));
});

describe('visibleRationaleTags', () => {
	it('drops what the meta row already shows', () => {
		// The engine's whole vocabulary, from apps/dj_copilot/suggester.py.
		assert.deepEqual(
			visibleRationaleTags([
				'bpm_match',
				'bpm_close',
				'camelot_step_0',
				'camelot_step_1_2',
				'energy_match'
			]),
			[]
		);
	});

	it('keeps pairing tags, which nothing else on the tile says', () => {
		assert.deepEqual(visibleRationaleTags(['bpm_match', 'pair_manual']), ['pair_manual']);
		assert.deepEqual(
			visibleRationaleTags(['pair_manual', 'pair_learned', 'pair_ai']),
			['pair_manual', 'pair_learned', 'pair_ai']
		);
	});

	it('keeps an unrecognised tag rather than hiding a new rationale', () => {
		// Dropping by allow-list would mean a new engine tag never appears.
		assert.deepEqual(visibleRationaleTags(['vocal_clash_free']), ['vocal_clash_free']);
	});

	it('preserves order and handles the empty case', () => {
		assert.deepEqual(visibleRationaleTags([]), []);
		assert.deepEqual(visibleRationaleTags(['pair_ai', 'zzz']), ['pair_ai', 'zzz']);
	});
});
