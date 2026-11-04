// requirement: DIALCLOCK-8341
// [if] a dial is at 0.5 [then] it reads 12 o'clock, 0 reads 7:30 and 1 reads 4:30 (the 270-degree sweep Knob.svelte draws)
// [if] an agent says 3pm, 3 o'clock, 3:00, 15:00, 3am or 3 [then] all of them are the same position, 0.8333
// [if] a clock time falls in the dead gap at the bottom of the dial (5, 6, 7 o'clock) [then] it is refused by name, never clamped
// [if] Knob.svelte draws its sweep [then] it uses DIAL_SWEEP_DEG, so the clock map and the drawn needle cannot drift
import assert from 'node:assert/strict';
import { before, describe, test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let clock;

before(async () => {
	clock = await loadTypeScriptModule('src/lib/rb/dial-clock.ts');
});

const close = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} != ${expected}`);

describe('dial value <-> clock time', () => {
	test('the landmarks of the 270-degree dial', () => {
		assert.equal(clock.DIAL_SWEEP_DEG, 270);
		assert.equal(clock.dialClockLabel(0.5), "12 o'clock");
		assert.equal(clock.dialClockLabel(0), '7:30');
		assert.equal(clock.dialClockLabel(1), '4:30');
		assert.equal(clock.dialClockLabel(5 / 6), "3 o'clock");
		assert.equal(clock.dialClockLabel(1 / 6), "9 o'clock");
		close(clock.dialValueFromClock('12'), 0.5);
		close(clock.dialValueFromClock('7:30'), 0);
		close(clock.dialValueFromClock('4:30'), 1);
	});

	test('every spelling of three o clock is the same position', () => {
		for (const text of ['3', '3pm', '3 pm', '3am', '3PM', "3 o'clock", '3 oclock', "3o'clock", '3:00', '15:00', '15']) {
			close(clock.dialValueFromClock(text), 5 / 6);
		}
		for (const text of ['9', '9pm', '21:00', "9 o'clock"]) close(clock.dialValueFromClock(text), 1 / 6);
		close(clock.dialValueFromClock('0:00'), 0.5);
		close(clock.dialValueFromClock('24:00'), 0.5);
	});

	test('round trip: every 5-minute mark on the dial maps back to itself', () => {
		for (let minutes = -270; minutes <= 270; minutes += 5) {
			const value = 0.5 + minutes / 540;
			const label = clock.dialClockLabel(value);
			close(clock.dialValueFromClock(label), value);
		}
	});

	test('the dead gap and non-times are refused by name', () => {
		for (const text of ['5', '6', "7 o'clock", '4:45', '19:00']) {
			assert.throws(() => clock.dialValueFromClock(text), /past the dial's stops/, text);
		}
		for (const text of ['', 'noon', '3:5', '25', '3:60', 'three']) {
			assert.throws(() => clock.dialValueFromClock(text), TypeError, text);
		}
		assert.throws(() => clock.dialClockLabel(1.2), RangeError);
		assert.throws(() => clock.dialClockLabel(Number.NaN), RangeError);
	});

	test('Knob.svelte draws its needle with the same sweep', () => {
		const source = readFrontendSource('src/lib/components/rb/mixer/Knob.svelte');
		assert.match(source, /const SWEEP_DEG = DIAL_SWEEP_DEG;/);
	});
});
