// requirement: BEATFX-14254
// [if] planQuantizedTransition with quantize off takes a NaN or +-Infinity nowSec without a RangeError naming nowSec [then] broken
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const { planQuantizedTransition } = await loadTypeScriptModule('src/lib/rb/beat-fx-quantize.ts');

const GRID = Array.from({ length: 8 }, (_, k) => 0.1 + k * 0.5);

// [if] quantize is off and nowSec is NaN or +-Infinity [then] planning throws a RangeError naming nowSec, [else stop].
test('quantize off refuses a non-finite nowSec', () => {
	for (const nowSec of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
		assert.throws(
			() => planQuantizedTransition({ quantize: false, gridBeatsSec: GRID, nowSec, beats: 1 }),
			(err) => err instanceof RangeError && /nowSec/.test(err.message),
			`if quantize off accepts nowSec ${nowSec} then broken`
		);
	}
	assert.deepEqual(
		planQuantizedTransition({ quantize: false, gridBeatsSec: GRID, nowSec: 0.83, beats: 1 }),
		{ state: 'off', atSec: 0.83 }
	);
});
