/**
 * Issue #10780 (S3): the pure plan a SamplerVoice plays (spec section 4.3, 4.4), ported from the v1
 * reference. A slot becomes one plan from the launch rules, the start skips the clip guard so beat 1
 * lands on the launch time, and a play-once voice stops after exactly one region.
 *
 * Regression lines:
 * - if a 120 BPM slot plays against 126 with key auto then the plan is rate 1.05 with master tempo on
 * - if no master plays then the plan runs free at rate 1
 * - if the slot is one shot then the plan never loops
 * - if the plan plays once then the plain source duration is the region and the stretcher stops after it
 * - if the plan loops then loop points are clip seconds (guard plus window) and no stop is scheduled
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let P;
let api;
before(async () => {
	P = await loadTypeScriptModule('src/lib/player/sampler/sampler-voice-plan.ts');
	api = await loadTypeScriptModule('src/lib/player/sampler/api-sampler.ts');
});

const buffer = { name: 'clip' };
// One bar at 120 BPM: 4 beats over 2 s.
const slot = (settings = {}) => ({
	native_bpm: 120,
	key: '8A',
	in_ms: 10_000,
	out_ms: 12_000,
	beats: 4,
	settings: {
		launch: 'bar',
		key: 'auto',
		tempo: 'match',
		mode: 'loop',
		repeat: 'loop',
		downbeat_offset: 0,
		level: 80,
		...settings
	}
});
const near = (a, b) => Math.abs(a - b) < 1e-9;

//----- buildVoicePlan

test('if a 120 BPM slot plays against 126 with key auto then the plan is rate 1.05 with master tempo on', () => {
	/** [if] master is 126 BPM in the slot's key [then] rate 1.05, 0 semitones, master tempo on, [else stop]. */
	const plan = P.buildVoicePlan(slot(), buffer, { bpm: 126, key: '8A' });
	assert.ok(near(plan.rate, 1.05), String(plan.rate));
	assert.equal(plan.semitones, 0);
	assert.equal(plan.masterTempo, true);
	assert.equal(plan.buffer, buffer);
	assert.equal(plan.regionSec, 2);
	assert.equal(plan.guardSec, api.CLIP_GUARD_MS / 1000);
});

test('if no master plays then the plan runs free at rate 1', () => {
	/** [if] the master is null [then] the plan's rate is 1 (native tempo), [else stop]. */
	assert.equal(P.buildVoicePlan(slot(), buffer, null).rate, 1);
});

test('if the slot is vinyl then pitch follows rate on a plain source', () => {
	/** [if] the key mode is off [then] masterTempo is false and semitones follow the rate, [else stop]. */
	const plan = P.buildVoicePlan(slot({ key: 'off' }), buffer, { bpm: 126, key: null });
	assert.equal(plan.masterTempo, false);
	assert.ok(near(plan.semitones, 12 * Math.log2(1.05)), String(plan.semitones));
});

test('if the slot is one shot then the plan never loops', () => {
	/** [if] mode is one_shot with repeat loop [then] the plan does not loop and spans the region, [else stop]. */
	const plan = P.buildVoicePlan(slot({ mode: 'one_shot', repeat: 'loop' }), buffer, null);
	assert.equal(plan.loop, false);
	assert.equal(plan.loopEndSec, 2);
});

test('if repeat is 2 beats on a 4-beat region then the loop window is half the region', () => {
	/** [if] repeat is 2 on 4 beats over 2 s [then] the loop is 0..1 s and one pass lasts 1 s / rate, [else stop]. */
	const plan = P.buildVoicePlan(slot({ repeat: 2 }), buffer, { bpm: 126, key: '8A' });
	assert.deepEqual([plan.loop, plan.loopStartSec, plan.loopEndSec], [true, 0, 1]);
	assert.ok(near(P.passSecOf(plan), 1 / 1.05), String(P.passSecOf(plan)));
});

//----- source arguments

test('if the plan plays once then the plain source duration is the region and the stretcher stops after it', () => {
	/** [if] the plan plays once [then] offset is the guard, duration the region, stop at at + region / rate, [else stop]. */
	const plan = P.buildVoicePlan(slot({ repeat: 'once', key: 'off' }), buffer, {
		bpm: 126,
		key: null
	});
	const args = P.plainSourceArgs(plan);
	assert.equal(args.offsetSec, plan.guardSec);
	assert.equal(args.durationSec, 2);
	assert.ok(near(P.stretchStopAt(100, plan), 100 + 2 / 1.05));
	assert.deepEqual(
		[P.stretchLaunchChange(plan).loopStart, P.stretchLaunchChange(plan).loopEnd],
		[0, 0]
	);
});

test('if the plan loops then loop points are clip seconds (guard plus window) and no stop is scheduled', () => {
	/** [if] the plan loops [then] loop points add the guard, duration is null and no stop is set, [else stop]. */
	const plan = P.buildVoicePlan(slot({ repeat: 4 }), buffer, { bpm: 126, key: '8A' });
	const g = plan.guardSec;
	const args = P.plainSourceArgs(plan);
	assert.deepEqual(args, { offsetSec: g, durationSec: null, loopStartSec: g, loopEndSec: g + 2 });
	assert.equal(P.stretchStopAt(100, plan), null);
	assert.deepEqual(P.stretchLaunchChange(plan), {
		active: true,
		input: g,
		rate: plan.rate,
		semitones: 0,
		loopStart: g,
		loopEnd: g + 2
	});
});

test('if the stop ramp is read then it is the spec 5 ms', () => {
	/** [if] VOICE_STOP_RAMP_SEC is read [then] it is 0.005 s, shorter than the 50 ms launch lead, [else stop]. */
	assert.equal(P.VOICE_STOP_RAMP_SEC, 0.005);
});
