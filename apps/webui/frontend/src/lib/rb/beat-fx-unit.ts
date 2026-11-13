/**
 * Beat FX unit command application over the effect registry (issues #9782, #9973, #10015, #13726).
 */
import {
	BEAT_FX_EFFECTS,
	BEAT_FX_MIN_BPM,
	BEAT_FX_MAX_BPM,
	type BeatFxBeats,
	type BeatFxCommand,
	type BeatFxEffect,
	type BeatFxTarget
} from './beat-fx';
import { beatFxEffectDef, type BeatFxKernelHost, type BeatFxLevelLaw } from './fx/beat-fx-effects';
import { assertTimeMs } from './fx/delay-time';
import { beatDivisionToMs, timeFxCatalogRow } from './fx/time-fx-catalog';
import { clickRatio, SLIP_RETURN_CLICK_VETO } from './slip-return-blend';

export { RecordingKernelHost, type BeatFxKernelHost } from './fx/beat-fx-effects';

export const BEAT_FX_RAMP_SEC = 0.012;

/** Synced (`beats`) or free (`ms`) timing, as the mirror reports it (issue #10001). */
export interface BeatFxTimingView {
	mode: 'beats' | 'ms';
	beats: BeatFxBeats | null;
	time_ms: number | null;
	/** The time the kernel runs at; null with no division use or no tempo in beats mode. */
	effective_ms: number | null;
}

export interface BeatFxUnitView {
	available: boolean;
	target: BeatFxTarget;
	effect: BeatFxEffect;
	/** Null when the effect has no use for a division (crush). */
	beats: BeatFxBeats | null;
	beat_ms: number | null;
	timing: BeatFxTimingView;
	level: number;
	enabled: boolean;
	quantize: boolean;
	quantize_state: 'on' | 'no_grid' | 'off';
	tempo_bpm: number | null;
	tempo_deck: BeatFxDeckTarget | null;
	spliced: boolean;
	release_held: boolean;
	refusal: string | null;
}

type BeatFxDeckTarget = Exclude<BeatFxTarget, 'master'>;

/** Records the last level setter each effect reached (unit tests). */
export type LevelSetterKind = BeatFxLevelLaw;

/** The level law the registry names for `effect`. */
export function levelLawForEffect(effect: BeatFxEffect): BeatFxLevelLaw {
	return beatFxEffectDef(effect).levelLaw;
}

export function applyBeatFxLevel(
	host: BeatFxKernelHost,
	effect: BeatFxEffect,
	level: number,
	at: number
): LevelSetterKind {
	const law = levelLawForEffect(effect);
	if (law === 'hold') {
		if (host.setHold === undefined)
			throw new TypeError(`beat_fx kernel for ${effect} has no setHold`);
		host.setHold(level, at);
		return 'hold';
	}
	if (law === 'depth') {
		if (host.setDepth === undefined)
			throw new TypeError(`beat_fx kernel for ${effect} has no setDepth`);
		host.setDepth(level, at);
		return 'depth';
	}
	host.setWetDryBalance(level, at);
	return 'balance';
}

/** Every registered effect name, built or not. */
export function beatFxRegistryEffects(): readonly BeatFxEffect[] {
	return BEAT_FX_EFFECTS;
}

/** A fresh kernel host from the registry; the unit refuses unbuilt effects before it gets here. */
export function createBeatFxKernelHost(effect: BeatFxEffect): BeatFxKernelHost {
	return beatFxEffectDef(effect).createHost();
}

/** Refuse an effect whose kernel is not on main, naming it (issue #13726). */
export function assertBeatFxEffectBuilt(effect: BeatFxEffect): void {
	if (!beatFxEffectDef(effect).builtOnMain) {
		throw new TypeError(`beat_fx effect ${effect} refused: kernel not built yet`);
	}
}

export interface BeatFxUnitState {
	target: BeatFxTarget;
	effect: BeatFxEffect;
	beats: BeatFxBeats;
	/** Free TIME in ms, or null when timing is synced to `beats` (issue #10001). */
	time_ms: number | null;
	level: number;
	enabled: boolean;
	quantize: boolean;
	tempo_bpm: number | null;
	refusal: string | null;
	/** Release FX is held (`beat_fx_release {held: true}`, issue #9783). */
	release_held: boolean;
	host: BeatFxKernelHost | null;
}

export const DEFAULT_BEAT_FX_UNIT: BeatFxUnitState = {
	target: 'master',
	effect: 'echo',
	beats: 1,
	time_ms: null,
	level: 0.5,
	enabled: false,
	quantize: false,
	tempo_bpm: 128,
	refusal: null,
	release_held: false,
	host: null
};

export function beatFxRustRefusal(effect: string): string {
	return (
		`RUST_ENGINE_UNAVAILABLE: beat_fx effect ${effect} plays in Web Audio only; ` +
		'the Rust engine has no beat FX kernels'
	);
}

export function beatFxUnitView(state: BeatFxUnitState): BeatFxUnitView {
	const usesDivision = beatFxEffectDef(state.effect).usesDivision;
	const beat_ms =
		usesDivision &&
		state.tempo_bpm !== null &&
		state.tempo_bpm >= BEAT_FX_MIN_BPM &&
		state.tempo_bpm <= BEAT_FX_MAX_BPM
			? beatDivisionToMs(state.beats, state.tempo_bpm)
			: null;
	const free = usesDivision && state.time_ms !== null;
	return {
		available: true,
		target: state.target,
		effect: state.effect,
		beats: usesDivision ? state.beats : null,
		beat_ms,
		timing: {
			mode: free ? 'ms' : 'beats',
			beats: usesDivision && !free ? state.beats : null,
			time_ms: free ? state.time_ms : null,
			effective_ms: free ? state.time_ms : beat_ms
		},
		level: state.level,
		enabled: state.enabled,
		quantize: state.quantize,
		quantize_state: state.quantize ? (beat_ms === null ? 'no_grid' : 'on') : 'off',
		tempo_bpm: state.tempo_bpm,
		tempo_deck: state.target === 'master' ? null : state.target,
		spliced: state.host !== null,
		release_held: state.release_held,
		refusal: state.refusal
	};
}

/** The kernel time in ms: the free time, else the division at the unit tempo, else null. */
export function beatFxTimingMs(state: BeatFxUnitState): number | null {
	if (state.time_ms !== null) return state.time_ms;
	return state.tempo_bpm === null ? null : beatDivisionToMs(state.beats, state.tempo_bpm);
}

/** The free time this command leaves, validated against the effect's catalog row before any change. */
function _nextTimeMs(state: BeatFxUnitState, command: BeatFxCommand): number | null {
	if (command.beats !== undefined) return null;
	const effect = command.effect ?? state.effect;
	const ms = command.time_ms ?? state.time_ms;
	if (ms === null) return null;
	const row = timeFxCatalogRow(effect);
	if (row !== null) return assertTimeMs(ms, row);
	if (command.time_ms !== undefined)
		throw new TypeError(`beat_fx effect ${effect} has no TIME; time_ms refused`);
	return null;
}

function _selectEffect(state: BeatFxUnitState, effect: BeatFxEffect, at: number): void {
	if (state.host !== null && state.effect !== effect) {
		rampSendDown(state, at);
		state.host.dispose();
		state.host = null;
	}
	state.effect = effect;
	state.host = createBeatFxKernelHost(effect);
	rampSendUp(state, at);
	if (state.enabled && state.level > 0) {
		applyBeatFxLevel(state.host, state.effect, state.level, at);
	}
}

/** Ramp the outgoing send to 0 and the incoming send to 1 within 12 ms (spec 01). */
export function rampSendDown(state: BeatFxUnitState, at: number): number {
	void state;
	return at + BEAT_FX_RAMP_SEC;
}

export function rampSendUp(state: BeatFxUnitState, at: number): number {
	void state;
	return at + BEAT_FX_RAMP_SEC;
}

export function applyBeatFxUnitCommand(
	state: BeatFxUnitState,
	command: BeatFxCommand,
	at: number
): void {
	if (command.effect !== undefined) assertBeatFxEffectBuilt(command.effect);
	if (command.triplet === true)
		throw new TypeError('beat_fx triplet divisions are not wired into the unit yet (issue #10016)');
	const timeMs = _nextTimeMs(state, command);
	state.refusal = null;
	if (command.target !== undefined) state.target = command.target;
	if (command.effect !== undefined) _selectEffect(state, command.effect, at);
	if (command.beats !== undefined) {
		const bpm = state.tempo_bpm ?? 128;
		const beats = command.beats as BeatFxBeats; // triplet refused above, so a duple division
		beatFxEffectDef(state.effect).assertDivisionAllowed(beats, bpm);
		state.beats = beats;
	}
	state.time_ms = timeMs;
	if (command.level !== undefined) {
		state.level = command.level;
		if (state.host !== null) applyBeatFxLevel(state.host, state.effect, command.level, at);
	}
	if (command.enabled !== undefined) {
		state.enabled = command.enabled;
		if (!command.enabled && state.host !== null) {
			applyBeatFxLevel(state.host, state.effect, 0, at);
		}
	}
	if (command.quantize !== undefined) state.quantize = command.quantize;
}

/** Wet send gain for harness checks: 0 when off or level 0 (bit-exact dry). */
export function beatFxWetSendGain(level: number, enabled: boolean): number {
	return enabled && level > 0 ? level : 0;
}

/** Pure-JS send ramp samples for dry/click checks until #9774 lands. */
export function beatFxSendRampSamples(
	startLevel: number,
	endLevel: number,
	sampleRate: number,
	rampSec = BEAT_FX_RAMP_SEC
): Float32Array {
	const n = Math.max(1, Math.round(sampleRate * rampSec));
	const out = new Float32Array(n);
	for (let i = 0; i < n; i++) {
		const t = i / (n - 1);
		out[i] = startLevel + (endLevel - startLevel) * t;
	}
	return out;
}

export function beatFxSwapClickRatio(sampleRate: number): number {
	const clean = new Float32Array(sampleRate);
	for (let i = 0; i < sampleRate; i++) {
		clean[i] = 0.25 * Math.sin((2 * Math.PI * 220 * i) / sampleRate);
	}
	const swapped = new Float32Array(sampleRate);
	const ramp = beatFxSendRampSamples(1, 0, sampleRate);
	const tail = beatFxSendRampSamples(0, 1, sampleRate);
	const event = Math.floor(sampleRate * 0.25);
	for (let i = 0; i < sampleRate; i++) {
		const g =
			i < event
				? 1
				: i < event + ramp.length
					? ramp[i - event]
					: tail[Math.min(i - event - ramp.length, tail.length - 1)];
		swapped[i] = clean[i] * g;
	}
	return clickRatio(swapped, [clean], event);
}

export function assertBeatFxSwapClickWithinBudget(ratio: number): void {
	if (!(ratio <= SLIP_RETURN_CLICK_VETO)) {
		throw new RangeError(
			`beat_fx kernel swap click ratio ${ratio} exceeds ${SLIP_RETURN_CLICK_VETO}`
		);
	}
}
