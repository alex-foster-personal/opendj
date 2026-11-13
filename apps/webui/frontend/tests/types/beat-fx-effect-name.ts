// requirement: BEATFX-14819
// Type-level test, checked by `pnpm check` (svelte-check covers tests/**/*.ts), issue #13726.
// [if] BeatFxEffect widens to string [then] a misspelled effect becomes assignable and a Refused<...> line below stops compiling, [else stop].
import type { BeatFxCommand, BeatFxEffect } from '$lib/rb/beat-fx';
import { BEAT_FX_EFFECTS } from '$lib/rb/beat-fx';

/** `true` when a value of type Value is assignable to Target. */
type Assignable<Value, Target> = [Value] extends [Target] ? true : false;
/** Compiles only for `false`: the assignment the verdict names is one the compiler refuses. */
type Refused<Verdict extends false> = Verdict;

export const built: readonly BeatFxEffect[] = ['echo', 'delay', 'pingpong'];
export const unbuilt: readonly BeatFxEffect[] = [
	'spiral',
	'helix',
	'reverse_delay',
	'multitap',
	'tape'
];
export const listed: BeatFxEffect | undefined = BEAT_FX_EFFECTS[0];

/** A misspelled effect name is not a BeatFxEffect. */
export type MisspelledEffectRefused = Refused<Assignable<'ecco', BeatFxEffect>>;

/** A beat_fx command naming a misspelled effect does not type. */
export type MisspelledCommandRefused = Refused<
	Assignable<{ type: 'beat_fx'; effect: 'pingpnog' }, BeatFxCommand>
>;
