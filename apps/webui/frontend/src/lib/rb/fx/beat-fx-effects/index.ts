/**
 * Beat FX effect registry (issue #13726). To add an effect: one new file in
 * this directory that calls `registerBeatFxEffect`, plus one import line in
 * the registration block below and its name in `BeatFxEffectName`. Order here
 * is the order of the effect list.
 */
import type { TimeFxEffectName } from '../time-fx-catalog';
import { registeredBeatFxEffectNames } from './registry';

// Registration block: one line per effect file.
import './time-effects';
import './lfo-filter-effect';
import './roll-effect';
import './crush-effect';
import './reverb-effect';

export { beatFxEffectDef, registerBeatFxEffect, type BeatFxEffectDef } from './registry';
export {
	RecordingKernelHost,
	type BeatFxKernel,
	type BeatFxKernelHost,
	type BeatFxLevelLaw
} from './kernel-host';

/**
 * Literal union of every name the registration block registers, so a misspelled
 * effect is a compile error. A new effect file adds its name here as one more
 * `| 'name'` term (time-effects.ts registers every `TimeFxEffectName`).
 */
export type BeatFxEffectName = TimeFxEffectName;

/** Every registered effect name; the CLI list `BEAT_FX_EFFECT_VALUES` mirrors it. */
export const BEAT_FX_EFFECT_NAMES = registeredBeatFxEffectNames() as readonly BeatFxEffectName[];
