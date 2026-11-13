// requirement: BEATFX-14258
// Grid validation of nextQuantizeBoundarySec (issue #14102 part B): a non-finite beat anywhere in
// the grid is refused, so quantize planning never schedules at NaN or silently skips the grid.
// [if] a grid with a NaN or +-Infinity beat at any position plans a boundary or null instead of throwing [then] broken
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const { nextQuantizeBoundarySec, BEAT_FX_BEATS } =
	await loadTypeScriptModule('src/lib/rb/beat-fx.ts');

// 124 BPM from 0.1 s, as beat-fx-quantize.test.mjs.
const BEAT_SEC = 60 / 124;
const GRID = Array.from({ length: 32 }, (_unused, k) => 0.1 + k * BEAT_SEC);
const NON_FINITE = [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY];
const POSITIONS = [0, 1, 4, GRID.length - 2, GRID.length - 1];
const NOW_SAMPLES = [0, GRID[0], GRID[5] + 0.01, GRID[GRID.length - 1], 100];

describe('nextQuantizeBoundarySec grid validation', () => {
	// [if] any grid beat is NaN or +-Infinity, at any position, nowSec and division [then] RangeError naming the grid, never NaN, [else stop].
	test('a non-finite grid beat throws a RangeError naming the grid and never returns NaN', () => {
		for (const v of NON_FINITE) {
			for (const at of POSITIONS) {
				const grid = GRID.map((t, i) => (i === at ? v : t));
				for (const nowSec of NOW_SAMPLES) {
					for (const beats of BEAT_FX_BEATS) {
						assert.throws(
							() => nextQuantizeBoundarySec(grid, nowSec, beats),
							(err) => err instanceof RangeError && /grid beat \d+/.test(err.message),
							`beat ${at} = ${v}, now ${nowSec}, beats ${beats}`
						);
					}
				}
			}
			assert.throws(() => nextQuantizeBoundarySec([v], 1, 1), RangeError, `lone beat ${v}`);
		}
		for (const nowSec of NOW_SAMPLES) {
			for (const beats of BEAT_FX_BEATS) {
				const at = nextQuantizeBoundarySec(GRID, nowSec, beats);
				assert.ok(at === null || Number.isFinite(at), `finite grid, now ${nowSec}: ${at}`);
			}
		}
	});
});
