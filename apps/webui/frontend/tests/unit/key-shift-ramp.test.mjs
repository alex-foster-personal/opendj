import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import {
	engineBlockAfter,
	readFrontendSource,
	SCHEDULE_DECK_SERIAL_ANCHOR
} from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let composeStretchSemitones;
let planKeyShiftMutation;
let planKeyShiftRamp;
let rampDurationSec;
let rampMaxCents;

before(async () => {
	({
		KEY_SHIFT_RAMP_DURATION_SEC: rampDurationSec,
		KEY_SHIFT_RAMP_MAX_CENTS: rampMaxCents,
		planKeyShiftMutation,
		planKeyShiftRamp
	} = await loadTypeScriptModule('src/lib/rb/audio-engine-guards.ts'));
	({ composeStretchSemitones } = await loadTypeScriptModule('src/lib/player/key/camelot.ts'));
});

function _pitchAt(steps, fromSemitones, atSec) {
	let pitch = fromSemitones;
	for (const step of steps) {
		if (step.offsetSec > atSec) break;
		pitch = step.keyShiftSemitones;
	}
	return pitch;
}

test('a live +3 st change ramps for 40-60 ms with at most 30 cents per render block', () => {
	const fromSemitones = 0;
	const steps = planKeyShiftRamp(fromSemitones, 3);
	const last = steps.at(-1);
	assert.ok(last.offsetSec >= 0.04 && last.offsetSec <= 0.06);
	assert.equal(last.keyShiftSemitones, 3);

	let previousPitch = fromSemitones;
	for (const step of steps) {
		assert.ok(Math.abs(step.keyShiftSemitones - previousPitch) * 100 <= rampMaxCents + 1e-9);
		previousPitch = step.keyShiftSemitones;
	}

	for (const sampleRate of [44_100, 48_000]) {
		const blockDurationSec = 128 / sampleRate;
		let priorBlockPitch = fromSemitones;
		for (let atSec = 0; atSec <= last.offsetSec + blockDurationSec; atSec += blockDurationSec) {
			const pitch = _pitchAt(steps, fromSemitones, atSec);
			assert.ok(
				Math.abs(pitch - priorBlockPitch) * 100 <= rampMaxCents + 1e-9,
				`${sampleRate} Hz block at ${atSec}s stepped from ${priorBlockPitch} to ${pitch}`
			);
			priorBlockPitch = pitch;
		}
	}
});

test('the 30 cent cap is named and bounds every public key-shift move', () => {
	assert.equal(rampDurationSec, 0.05);
	assert.equal(rampMaxCents, 30);
	for (let from = -12; from <= 12; from += 1) {
		for (let to = -12; to <= 12; to += 1) {
			let previous = from;
			for (const step of planKeyShiftRamp(from, to)) {
				assert.ok(Math.abs(step.keyShiftSemitones - previous) * 100 <= rampMaxCents + 1e-9);
				previous = step.keyShiftSemitones;
			}
		}
	}
});

test('a paused deck applies the key shift immediately without entering the ramp path', () => {
	assert.deepEqual(
		planKeyShiftMutation(
			{
				playing: false,
				audible: false,
				controlActive: false,
				presentationPending: false,
				pendingScheduleCount: 0,
				scheduleIntentCount: 0
			},
			false,
			3
		),
		{ kind: 'immediate', active: false, publishedKeyShiftSemitones: 3 }
	);

	const body = engineBlockAfter(
		'async function _setDeckKeyShift(deck: DeckId, keyShiftSemitones: number): Promise<void> {'
	);
	const scheduledAt = body.indexOf('const fromSemitones =');
	assert.notEqual(scheduledAt, -1);
	assert.equal(body.slice(0, scheduledAt).includes('planKeyShiftRamp('), false);
	assert.match(body.slice(scheduledAt), /planKeyShiftRamp\(fromSemitones, keyShiftSemitones\)/);
});

test('the key ramp uses its own constants and is sequenced through the deck scheduler', () => {
	const plannerSource = readFrontendSource('src/lib/rb/audio-engine-guards.ts');
	assert.equal(plannerSource.includes('REANCHOR_RAMP_DURATION_SEC'), false);
	assert.equal(plannerSource.includes('0.25'), false);

	const body = engineBlockAfter(
		'async function _setDeckKeyShift(deck: DeckId, keyShiftSemitones: number): Promise<void> {'
	);
	assert.match(body, /const steps = planKeyShiftRamp\(/);
	assert.match(body, /const generation = _bumpKeyShiftRampOperation\(deck\)/);
	assert.match(body, /for \(const step of steps\)[\s\S]*await _scheduleDeck\(/);
	assert.match(body, /step\.keyShiftSemitones[\s\S]*generation/);
});

test('later key-ramp steps keep their requested offsets instead of collapsing', () => {
	const body = engineBlockAfter(SCHEDULE_DECK_SERIAL_ANCHOR);
	assert.match(
		body,
		/keyRampGeneration !== undefined[\s\S]*safeRequestedWhen > latestPendingStart[\s\S]*pendingBoundary/
	);
});

test('Signalsmith composition accepts fractional ramp samples but keeps the pitch bound', () => {
	assert.equal(composeStretchSemitones(1, true, 0.3), 0.3);
	assert.throws(() => composeStretchSemitones(1, true, 13), /-12\.\.12/i);
});
