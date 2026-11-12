/**
 * STEMPERF-11699: the level gestures on a deck stem chip, as pure logic.
 *
 * A wheel over a live chip sets that stem's level through the shared
 * `wheelAdjust` action; a double-click returns it to unity (knob 0.5) and never
 * mutes. Off unity the chip shows a thin bottom bar whose height follows the
 * knob, and the tooltip carries the level in dB.
 *
 * No DOM and no import of wheel-adjust.ts (that module pulls in the api
 * client); the component passes WHEEL_STEP.knob in.
 *
 * Acceptance:
 * - [if] knob is 0.5 (or 0.5 within float noise) [then] unity, no bar, '0.0 dB'
 * - [if] knob is 0.25 / 0.75 [then] '-6.0 dB' / '+3.0 dB', bar 1px / 3px
 * - [if] the chip is not live [then] the wheel params are disabled
 * - [if] a plain click lands, at or off unity [then] mute fires at once, never deferred (Class A)
 * - [if] a double-click lands off unity [then] stem_gain 0.5 from the dblclick, no stem_mute from it, mute ends where it started
 */
import { stemLinearFromKnob } from '$lib/player/constants';
import type { StemControl } from '$lib/rb/stem-types';

export const STEM_UNITY_KNOB = 0.5;
export const STEM_CHIP_BAR_MAX_PX = 4;

const UNITY_TOLERANCE = 1e-6;

//-----------------------------------------------------------------------------
// level view
//-----------------------------------------------------------------------------

export function stemChipIsUnity(knob: number): boolean {
	return Math.abs(knob - STEM_UNITY_KNOB) < UNITY_TOLERANCE;
}

export function stemChipDbLabel(knob: number): string {
	const lin = stemLinearFromKnob(knob);
	if (lin === 0) return '-inf dB';
	const db = Math.round(20 * Math.log10(lin) * 10) / 10;
	if (db === 0) return '0.0 dB';
	if (db > 0) return `+${db.toFixed(1)} dB`;
	return `-${Math.abs(db).toFixed(1)} dB`;
}

export type StemChipLevelView = { unity: boolean; barPx: number; dbLabel: string };

export function stemChipLevelView(knob: number): StemChipLevelView {
	return {
		unity: stemChipIsUnity(knob),
		barPx: knob * STEM_CHIP_BAR_MAX_PX,
		dbLabel: stemChipDbLabel(knob)
	};
}

//-----------------------------------------------------------------------------
// wheel
//-----------------------------------------------------------------------------

/** Structurally a WheelAdjustParams. */
export type StemChipWheelParams = {
	step: number;
	get: () => number;
	set: (v: number) => void;
	disabled: boolean;
};

export function stemChipWheelParams(a: {
	stem: StemControl;
	gain: () => number;
	live: boolean;
	step: number;
	onGain: (stem: StemControl, value: number) => void;
}): StemChipWheelParams {
	return { step: a.step, get: a.gain, set: (v) => a.onGain(a.stem, v), disabled: !a.live };
}

//-----------------------------------------------------------------------------
// click vs double-click
//-----------------------------------------------------------------------------

export type ChipClick = { stem: StemControl; shift: boolean };
export type ChipClickArbiter = {
	click(c: ChipClick): void;
	doubleClick(stem: StemControl): void;
};

/** Every click dispatches at once (Class A, LATENCY-01). A double-click's two
 * clicks each toggle mute, so the pair leaves mute unchanged; the dblclick
 * itself only resets gain to unity. */
export function createChipClickArbiter(deps: {
	onMute: (stem: StemControl) => void | Promise<void>;
	onSolo: (stem: StemControl) => void | Promise<void>;
	onGain: (stem: StemControl, value: number) => void;
}): ChipClickArbiter {
	return {
		click(c) {
			if (c.shift) {
				void deps.onSolo(c.stem);
			} else {
				void deps.onMute(c.stem);
			}
		},
		doubleClick(stem) {
			deps.onGain(stem, STEM_UNITY_KNOB);
		}
	};
}
