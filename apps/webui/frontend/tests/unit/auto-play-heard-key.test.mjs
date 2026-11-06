// requirement: AUTOPLAY-8868, AUTOPLAY-8868c
// [if] the master plays shifted [then] AutoPlay picks against the key it is heard in, [else stop].
// [if] a track fits the heard key only after a shift within the user's max [then] it is picked and loads shifted, [else stop].
// [if] no shift within the max fits [then] the track is not a key match, [else stop].
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let chain;
let camelot;

before(async () => {
	chain = await loadTypeScriptModule('src/lib/rb/auto-play-chain.ts');
	camelot = await loadTypeScriptModule('src/lib/player/key/camelot.ts');
});

const row = (stable_id, key, bpm = 120) => ({ stable_id, key, bpm, file_exists: true });

test('Monster (9A, playing +1 = 4A) to Sun (10A): Sun loads +1 to fit the heard key', () => {
	const heard = camelot.effectiveCamelotKey('9A', 1);
	assert.equal(heard, '4A', 'if the heard key of 9A +1 is not 4A then broken');
	assert.equal(chain.autoPlayKeyShift('10A', heard, 1), 1, 'if Sun does not load +1 against 4A then broken');
	assert.equal(chain.autoPlayKeyShift('10A', heard, 0), null, 'if a 6-step clash passes with no shift allowed then broken');
	assert.equal(chain.autoPlayKeyShift('10A', '9A', 1), 0, 'control: unshifted 10A fits stored 9A with no shift');
});

test('the picker shifts instead of avoiding, and stays as before with no shift allowed', () => {
	const input = {
		playlist: [row('monster', '9A'), row('sun', '10A')],
		current_stable_id: 'monster',
		current_key: '4A',
		current_bpm: 120,
		exclude_ids: new Set(),
		played_ids: new Set(),
		enforce_play_order: false,
		min_tempo_ratio: 0.92,
		max_tempo_ratio: 1.08
	};
	assert.equal(chain.pickNextStableId(input), null, 'control: with no shift the heard clash is not picked');
	assert.equal(chain.pickNextStableId({ ...input, key_shift_max: 1 }), 'sun', 'if a shift-fit is avoided then broken');
	assert.equal(
		chain.pickNextStableId({ ...input, key_shift_max: 1, maximize_reach: true }),
		'sun',
		'if maximize-reach ignores the shift then broken'
	);
});

test('maximize-reach degree uses the key after load shift (4A: prefer 5A over 8A)', () => {
	// Order matches issue #8879 (1A, 8A, 5A) plus a later 1A so 5A has an onward hop from 5A (heard) that 8A (heard 3A) does not.
	const playlist = [row('one', '4A'), row('t1a', '1A'), row('eight', '8A'), row('five', '5A'), row('t1b', '1A')];
	const base = {
		playlist,
		current_stable_id: 'one',
		current_key: '4A',
		current_bpm: 120,
		exclude_ids: new Set(),
		played_ids: new Set(),
		enforce_play_order: false,
		min_tempo_ratio: 0.92,
		max_tempo_ratio: 1.08,
		max_steps: 10_000,
		key_shift_max: 1,
		maximize_reach: true
	};
	assert.equal(chain.pickNextStableId(base), 'five', 'if 8A wins by stored key the onward degree is wrong');
	const orderedFromOne = playlist.slice(1).concat(playlist.slice(0, 1));
	const candidates = orderedFromOne.filter((r) => r.stable_id !== 'one');
	assert.equal(
		chain.pickNextMaximizingReach({ ...base, candidates }).next,
		'five',
		'if Warnsdorff does not use shifted candidate key then broken'
	);
	assert.notEqual(
		chain.pickNextStableId({ ...base, key_shift_max: 0 }),
		'eight',
		'control: without shift 8A is not a candidate from 4A'
	);
});

test('the charted queue shows the track the shifted pick loads, from the heard key', () => {
	const playlist = [row('monster', '9A'), row('sun', '10A'), row('next', '6A')];
	const walk = (key_shift_max) => [
		...chain.simulateAutoPlayChain({
			playlist,
			start_stable_id: 'monster',
			start_key: '9A',
			start_heard_key: '4A',
			start_bpm: 120,
			enforce_play_order: false,
			maximize_reach: false,
			min_tempo_ratio: 0.92,
			max_tempo_ratio: 1.08,
			key_shift_max
		})
	];
	assert.deepEqual(walk(1).slice(0, 2), ['monster', 'sun'], 'if the queue disagrees with the shifted pick then broken');
	assert.equal(walk(0).includes('sun'), false, 'control: with no allowance the heard 4A does not chart Sun');
	// The chain keeps walking after the shifted hop (Sun heard at 5A).
	assert.deepEqual(walk(1), ['monster', 'sun', 'next']);
});
