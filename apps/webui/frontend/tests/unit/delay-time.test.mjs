// requirement: BEATFX-14616
// Beat FX free TIME in ms, pure helpers (issue #10001, spec fx-fanout-20261108/04 section 3).
import assert from 'node:assert/strict';
import { before, describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let time;
let catalog;

before(async () => {
	[time, catalog] = await Promise.all([
		loadTypeScriptModule('src/lib/rb/fx/delay-time.ts'),
		loadTypeScriptModule('src/lib/rb/fx/time-fx-catalog.ts')
	]);
});

describe('delay-time helpers', () => {
	test('beatsToSeconds gives the division length and refuses a bad tempo', () => {
		// [if] 1 beat at 120 BPM is not 0.5 s, 3/4 at 128 not 0.3515625 s, or bpm 0 passes [then] synced timing is wrong, [else stop].
		assert.equal(time.beatsToSeconds(1, 120), 0.5);
		assert.equal(time.beatsToSeconds(3 / 4, 128), 0.3515625);
		assert.throws(() => time.beatsToSeconds(1, 0), /not finite and positive/);
	});

	test('msToNearestBeats picks the nearest division and the error in percent of a beat', () => {
		// [if] 375 ms at 120 BPM is not 3/4 at +0%, or 400 ms not 3/4 at +5%, or 0 ms passes [then] the ms readout lies, [else stop].
		assert.deepEqual(time.msToNearestBeats(375, 120), { beats: 3 / 4, errorPercentOfBeat: 0 });
		assert.deepEqual(time.msToNearestBeats(400, 120), { beats: 3 / 4, errorPercentOfBeat: 5 });
		assert.deepEqual(time.msToNearestBeats(4000, 120).beats, 8);
		assert.equal(time.msToNearestBeats(1, 120).beats, 1 / 16);
		assert.throws(() => time.msToNearestBeats(0, 120), /time_ms 0/);
	});

	test('divisionLabel names every division from 1/16 to 16 and refuses one off the list', () => {
		// [if] a division lacks its 1/16..16 or 3/4 label, or 1/3 gets a label [then] the panel shows a wrong division, [else stop].
		assert.deepEqual([1 / 16, 1 / 8, 1 / 4, 1 / 2, 3 / 4, 1, 2, 4, 8, 16].map(time.divisionLabel), [
			'1/16',
			'1/8',
			'1/4',
			'1/2',
			'3/4',
			'1',
			'2',
			'4',
			'8',
			'16'
		]);
		assert.throws(() => time.divisionLabel(1 / 3), /beats 0\.333/);
	});

	test('xpadToMs maps 0..1 on a log scale from 1 to 4000 ms and refuses off the pad', () => {
		// [if] x 0 is not 1 ms, x 1 not 4000 ms, x 0.5 not the geometric mean, or x 1.1 passes [then] the pad is not log, [else stop].
		assert.equal(time.xpadToMs(0), 1);
		assert.equal(time.xpadToMs(1), 4000);
		assert.ok(Math.abs(time.xpadToMs(0.5) - Math.sqrt(4000)) < 1e-9);
		for (const x of [0.1, 0.3, 0.7, 0.9]) {
			assert.ok(time.xpadToMs(x) < time.xpadToMs(x + 0.05), `monotonic at ${x}`);
		}
		assert.throws(() => time.xpadToMs(1.1), /xpad must be a number in 0\.\.1/);
		assert.throws(() => time.xpadToMs(Number.NaN), /xpad/);
	});

	test('assertTimeMs takes the catalog range and names the value and the bound when refusing', () => {
		// [if] echo 1 or 4000 ms is refused, or echo 4001, spiral 5 or NaN passes unnamed [then] TIME bounds drift from the catalog, [else stop].
		const echo = catalog.timeFxCatalogRow('echo');
		const spiral = catalog.timeFxCatalogRow('spiral');
		assert.equal(time.assertTimeMs(1, echo), 1);
		assert.equal(time.assertTimeMs(4000, echo), 4000);
		assert.throws(() => time.assertTimeMs(4001, echo), /echo time_ms 4001 outside 1\.\.4000 ms/);
		assert.throws(() => time.assertTimeMs(5, spiral), /spiral time_ms 5 outside 10\.\.4000 ms/);
		assert.throws(() => time.assertTimeMs(Number.NaN, echo), /finite number; got NaN/);
	});
});
