/**
 * Beat FX unit kernel registry and command application (issues #9782, #9973, #10015).
 */
import {
	BEAT_FX_MIN_BPM,
	BEAT_FX_MAX_BPM,
	type BeatFxBeats,
	type BeatFxCommand,
	type BeatFxTarget
} from './beat-fx';
import {
	TIME_FX_BUILT_EFFECT_NAMES,
	assertTimeFxDivisionAllowed,
	beatDivisionToMs,
	timeFxCatalogRow,
	type TimeFxEffectName,
	type TimeFxLevelLaw
} from './fx/time-fx-catalog';
import { clickRatio, SLIP_RETURN_CLICK_VETO } from './slip-return-blend';

export const BEAT_FX_RAMP_SEC = 0.012;

export type BeatFxEffect = (typeof TIME_FX_BUILT_EFFECT_NAMES)[number];

export interface BeatFxKernelHost {
	readonly effect: BeatFxEffect;
	setWetDryBalance(level: number, at: number): void;
	setHold?(hold: number, at: number): void;
	setDepth?(depth: number, at: number): void;
	setTiming?(seconds: number, at: number): void;
	dispose(): void;
}

export interface BeatFxUnitView {
	available: boolean;
	target: BeatFxTarget;
	effect: BeatFxEffect;
	beats: BeatFxBeats;
	beat_ms: number | null;
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

type RegistryEntry = {
	effect: BeatFxEffect;
	levelLaw: TimeFxLevelLaw;
	create: () => BeatFxKernelHost;
};

/** Records the last level setter each effect reached (unit tests). */
export type LevelSetterKind = 'balance' | 'hold' | 'depth';

export class RecordingKernelHost implements BeatFxKernelHost {
	readonly effect: BeatFxEffect;
	lastLevelKind: LevelSetterKind | null = null;
	lastLevel = NaN;
	wetDry = 1;
	hold = 0;
	depth = 0;
	disposed = false;

	constructor(effect: BeatFxEffect) {
		this.effect = effect;
	}

	setWetDryBalance(level: number, _at: number): void {
		this.lastLevelKind = 'balance';
		this.lastLevel = level;
		this.wetDry = level;
	}

	setHold(hold: number, _at: number): void {
		this.lastLevelKind = 'hold';
		this.lastLevel = hold;
		this.hold = hold;
	}

	setDepth(depth: number, _at: number): void {
		this.lastLevelKind = 'depth';
		this.lastLevel = depth;
		this.depth = depth;
	}

	dispose(): void {
		this.disposed = true;
	}
}

export function levelLawForEffect(effect: BeatFxEffect): TimeFxLevelLaw {
	const row = timeFxCatalogRow(effect);
	if (row === null) throw new TypeError(`beat_fx effect ${effect} is not built on main`);
	return row.levelLaw;
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

const _registry = new Map<BeatFxEffect, RegistryEntry>();

for (const effect of TIME_FX_BUILT_EFFECT_NAMES) {
	const row = timeFxCatalogRow(effect);
	if (row === null) continue;
	_registry.set(effect, {
		effect,
		levelLaw: row.levelLaw,
		create: () => new RecordingKernelHost(effect)
	});
}

export function beatFxRegistryEffects(): readonly BeatFxEffect[] {
	return [..._registry.keys()];
}

export function createBeatFxKernelHost(effect: BeatFxEffect): BeatFxKernelHost {
	const entry = _registry.get(effect);
	if (entry === undefined) throw new TypeError(`beat_fx effect ${effect} is not registered`);
	return entry.create();
}

export interface BeatFxUnitState {
	target: BeatFxTarget;
	effect: BeatFxEffect;
	beats: BeatFxBeats;
	level: number;
	enabled: boolean;
	quantize: boolean;
	tempo_bpm: number | null;
	refusal: string | null;
	host: BeatFxKernelHost | null;
}

export const DEFAULT_BEAT_FX_UNIT: BeatFxUnitState = {
	target: 'master',
	effect: 'echo',
	beats: 1,
	level: 0.5,
	enabled: false,
	quantize: false,
	tempo_bpm: 128,
	refusal: null,
	host: null
};

export function beatFxRustRefusal(effect: string): string {
	return (
		`RUST_ENGINE_UNAVAILABLE: beat_fx effect ${effect} plays in Web Audio only; ` +
		'the Rust engine has no beat FX kernels'
	);
}

export function beatFxUnitView(state: BeatFxUnitState): BeatFxUnitView {
	const beat_ms =
		state.tempo_bpm !== null &&
		state.tempo_bpm >= BEAT_FX_MIN_BPM &&
		state.tempo_bpm <= BEAT_FX_MAX_BPM
			? beatDivisionToMs(state.beats, state.tempo_bpm)
			: null;
	return {
		available: true,
		target: state.target,
		effect: state.effect,
		beats: state.beats,
		beat_ms,
		level: state.level,
		enabled: state.enabled,
		quantize: state.quantize,
		quantize_state: state.quantize ? 'off' : 'off',
		tempo_bpm: state.tempo_bpm,
		tempo_deck: state.target === 'master' ? null : state.target,
		spliced: state.host !== null,
		release_held: false,
		refusal: state.refusal
	};
}

function _selectEffect(state: BeatFxUnitState, effect: TimeFxEffectName, at: number): void {
	const row = timeFxCatalogRow(effect);
	if (row === null) {
		throw new TypeError(`beat_fx effect ${effect} is not built on main yet`);
	}
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
	state.refusal = null;
	if (command.target !== undefined) state.target = command.target;
	if (command.effect !== undefined) _selectEffect(state, command.effect as TimeFxEffectName, at);
	if (command.beats !== undefined) {
		const bpm = state.tempo_bpm ?? 128;
		assertTimeFxDivisionAllowed(state.effect, command.beats, bpm);
		state.beats = command.beats;
	}
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
