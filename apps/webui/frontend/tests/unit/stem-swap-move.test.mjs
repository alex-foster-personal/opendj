import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let swap;

before(async () => {
	swap = await loadTypeScriptModule('src/lib/rb/stem-swap-move.ts');
});

const ready = (layout, playing = true) => ({ layout, stems_ready: true, playing });

test('two-bar demucs4 bass swap is two downbeat-anchored ramps', () => {
	/** [if] two ready demucs4 decks, B playing, bars 2 [then] A bass to 0 and B bass to 0.5 over 2 bars at next downbeat, [else stop]. */
	const plan = swap.stemSwapPlan({
		stem: 'bass',
		from: 1,
		to: 2,
		bars: 2,
		fromDeck: ready('demucs4'),
		toDeck: ready('demucs4')
	});
	assert.equal(plan.ok, true);
	assert.equal(plan.ramps.length, 2);
	const over = { unit: 'bars', n: 2, anchor: 'next_downbeat', clock: 'master' };
	assert.deepEqual(plan.ramps[0], {
		command: { type: 'stem_gain', deck: 1, stem: 'bass', value: 0 },
		to: 0,
		over
	});
	assert.deepEqual(plan.ramps[1], {
		command: { type: 'stem_gain', deck: 2, stem: 'bass', value: 0.5 },
		to: 0.5,
		over
	});
	assert.equal(swap.stemSwapDurationMs(2, 128), (2 * 4 * 60_000) / 128);
});

test('bars 0 is a 30 ms near-hard swap', () => {
	/** [if] bars is 0 [then] both ramps last 30 ms in ms units, [else stop]. */
	const plan = swap.stemSwapPlan({
		stem: 'vocal',
		from: 1,
		to: 2,
		bars: 0,
		fromDeck: ready('demucs4'),
		toDeck: ready('roformer2')
	});
	assert.equal(plan.ok, true);
	for (const ramp of plan.ramps) assert.deepEqual(ramp.over, { unit: 'ms', n: 30 });
	assert.equal(swap.stemSwapDurationMs(0, 128), 30);
});

test('refusals are named, never a silent no-op', () => {
	/** [if] B lacks the stem, B not ready, B stopped or from equals to [then] each refusal names its reason, [else stop]. */
	const req = (toDeck, extra = {}) =>
		swap.stemSwapPlan({
			stem: 'bass',
			from: 1,
			to: 2,
			fromDeck: ready('demucs4'),
			toDeck,
			...extra
		});
	const noBass = req(ready('roformer2'));
	assert.equal(noBass.ok, false);
	assert.match(noBass.reason, /has no bass control/);
	assert.deepEqual(req({ layout: 'demucs4', stems_ready: false, playing: true }), {
		ok: false,
		reason: 'stems_not_ready'
	});
	assert.deepEqual(req(ready('demucs4', false)), { ok: false, reason: 'to_deck_not_playing' });
	assert.deepEqual(req(ready('demucs4'), { to: 1 }), { ok: false, reason: 'same_deck' });
});

test('negative or fractional bars throws', () => {
	/** [if] bars is negative or fractional [then] stemSwapPlan throws a RangeError, [else stop]. */
	for (const bars of [-1, 1.5]) {
		assert.throws(
			() =>
				swap.stemSwapPlan({
					stem: 'bass',
					from: 1,
					to: 2,
					bars,
					fromDeck: ready('demucs4'),
					toDeck: ready('demucs4')
				}),
			RangeError
		);
	}
});
