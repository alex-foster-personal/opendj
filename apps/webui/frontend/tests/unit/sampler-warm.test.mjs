/**
 * Issue #10782 (S5 residency): the warm-vs-cold voice rules (EDGESAMP-11872i perf round 2).
 * A parked voice replays warm only on the same clip, context and output, and a trigger never
 * takes a live (playing or armed) voice while a free one exists.
 *
 * Regression lines:
 * - if a parked voice holds the same clip, context and output then it re-triggers warm
 * - if the voice is armed, disposed or lost its processor then it is never re-used
 * - if a free voice holds the clip then the pick prefers it, and a live voice is never picked
 * - if a launch lands inside the previous stop ramp then it throws
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let warm;
before(async () => {
	warm = await loadTypeScriptModule('src/lib/player/sampler/sampler-warm.ts');
});

const ctx = { name: 'ctx' };
const clip = { name: 'clip' };
const out = { name: 'master' };
const parked = (o) => ({
	warm: true,
	disposed: false,
	armed: false,
	hasProcessor: true,
	ctx,
	loaded: clip,
	destination: out,
	...o
});

test('if a parked warm voice holds the same clip, context and output then it re-triggers warm', () => {
	/** [if] the parked voice matches clip, ctx and output (or no output is asked) [then] it is re-used warm, [else stop]. */
	assert.equal(warm.canReuseWarm(parked({}), ctx, clip, out), true);
	assert.equal(warm.canReuseWarm(parked({}), ctx, clip), true);
});

test('if the clip differs then the voice rebuilds cold', () => {
	/** [if] the trigger asks for another clip [then] the parked voice is not re-used, [else stop]. */
	assert.equal(warm.canReuseWarm(parked({}), ctx, { name: 'other' }, out), false);
});

test('if the context differs then the voice rebuilds cold', () => {
	/** [if] the trigger runs on another AudioContext [then] the parked voice is not re-used, [else stop]. */
	assert.equal(warm.canReuseWarm(parked({}), { name: 'ctx2' }, clip, out), false);
});

test('if the output node differs then the voice rebuilds cold', () => {
	/** [if] the trigger routes to another output (cue instead of master) [then] it rebuilds cold, [else stop]. */
	assert.equal(warm.canReuseWarm(parked({}), ctx, clip, { name: 'cue' }), false);
});

test('if the processor errored (warmth dropped) then the voice rebuilds cold', () => {
	/** [if] the voice lost its warm flag [then] it is not re-used, [else stop]. */
	assert.equal(warm.canReuseWarm(parked({ warm: false }), ctx, clip, out), false);
});

test('if the voice is disposed, still armed or has no processor then it is not re-used', () => {
	/** [if] the voice is disposed, armed, processor-less or empty [then] canReuseWarm is false, [else stop]. */
	for (const o of [{ disposed: true }, { armed: true }, { hasProcessor: false }, { loaded: null }])
		assert.equal(warm.canReuseWarm(parked(o), ctx, clip, out), false, JSON.stringify(o));
});

test('if a free voice already holds the clip then the pick prefers it over an earlier free voice', () => {
	/** [if] voice 7 is free and holds the clip while 5 and 6 are free too [then] the pick is 7, [else stop]. */
	const id = warm.pickFreeVoice(
		[5, 6, 7, 8],
		(i) => i === 8,
		(i) => i === 7
	);
	assert.equal(id, 7);
});

test('if the holding voice is live then the pick takes the first free voice instead', () => {
	/** [if] the voice holding the clip is live [then] the pick is the first free voice, never a live one, [else stop]. */
	const id = warm.pickFreeVoice(
		[5, 6, 7, 8],
		(i) => i === 5 || i === 7,
		(i) => i === 7
	);
	assert.equal(id, 6);
});

test('if every voice is live then the pick returns undefined (caller steals the oldest)', () => {
	/** [if] all voices are live [then] pickFreeVoice returns undefined, [else stop]. */
	assert.equal(
		warm.pickFreeVoice(
			[5, 6],
			() => true,
			() => true
		),
		undefined
	);
});

test('if a launch lands inside the stop fade then it throws', () => {
	/** [if] the start time is before the previous stop ramp ends [then] assertLaunchClearsStopRamp throws, [else stop]. */
	assert.throws(() => warm.assertLaunchClearsStopRamp(1.005, 1.01), /inside its stop ramp/);
});

test('if a launch lands at or after the stop fade end then it is allowed', () => {
	/** [if] the start time is at or after the stop ramp end [then] no error is thrown, [else stop]. */
	warm.assertLaunchClearsStopRamp(1.01, 1.01);
	warm.assertLaunchClearsStopRamp(1.06, 1.01);
});
