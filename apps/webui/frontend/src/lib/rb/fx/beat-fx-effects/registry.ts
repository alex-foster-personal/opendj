/**
 * Beat FX effect registry storage (issue #13726). Effect files call
 * `registerBeatFxEffect`; `index.ts` imports each effect file once.
 */
import type { BeatFxKernel, BeatFxKernelHost, BeatFxLevelLaw } from './kernel-host';

/** One beat FX effect the unit can name. */
export interface BeatFxEffectDef {
	/** Wire name in `beat_fx {effect}` and the CLI list. */
	readonly name: string;
	readonly levelLaw: BeatFxLevelLaw;
	/** False when the kernel is not on main: the unit refuses the effect by name. */
	readonly builtOnMain: boolean;
	/** False when the beat division has no meaning (crush): the mirror then reports `beats: null`. */
	readonly usesDivision: boolean;
	/** Throws when the division or the time it gives at `bpm` is outside the effect's range. */
	assertDivisionAllowed(beats: number, bpm: number): void;
	createHost(): BeatFxKernelHost;
	/** The audio kernel for the unit's slot; absent while the kernel is not built. */
	createKernel?(ctx: BaseAudioContext): BeatFxKernel;
}

const _defs = new Map<string, BeatFxEffectDef>();

/** Add one effect; a second registration of the same name throws naming it. */
export function registerBeatFxEffect(def: BeatFxEffectDef): void {
	if (_defs.has(def.name)) {
		throw new TypeError(`beat_fx effect ${def.name} is already registered`);
	}
	_defs.set(def.name, Object.freeze({ ...def }));
}

/** Registered effect names, in registration order. */
export function registeredBeatFxEffectNames(): readonly string[] {
	return Object.freeze([..._defs.keys()]);
}

/** The registered effect for `name`; throws naming it when unknown. */
export function beatFxEffectDef(name: string): BeatFxEffectDef {
	const def = _defs.get(name);
	if (def === undefined) throw new TypeError(`beat_fx effect ${name} is not registered`);
	return def;
}
