/**
 * NOISE colour kernels (#9804 base, #10270 pink variant).
 *
 * Seeded in-repo stereo noise buffers (no audio files, no Math.random).
 * Pink is derived from the same white seed via a deterministic -3 dB/oct filter.
 */
import type { DeckId } from '$lib/player/constants';
import { onVariantChange } from '$lib/rb/variant-prefs';
import { NOISE_COLOUR_VARIANT } from './color-fx-noise-variants';

export { NOISE_COLOUR_VARIANT };

/** Matches COLOR_FX_RAMP_SEC in color-fx-graph.ts (avoid graph import at registry time). */
export const NOISE_BUFFER_RAMP_SEC = 0.01;

export const NOISE_OPEN_HZ = 1000;
export const NOISE_LOW_HZ = 100;
export const NOISE_HIGH_HZ = 16000;
export const NOISE_GAIN_MAX_DB = -6;
export const NOISE_LOOP_SECONDS = 4;
export const NOISE_DECORRELATION_MAX = 0.05;

export type NoiseColour = 'white' | 'pink';

const BASE_SEED = 0x5eed;

export function deckNoiseSeed(deck: DeckId): number {
	const n = typeof deck === 'number' ? deck : Number.parseInt(String(deck), 10);
	return (BASE_SEED ^ ((Number.isFinite(n) ? n : 0) * 0x9e3779b9)) >>> 0 || 1;
}

export function seededUnitRand(seed: number): () => number {
	let state = seed >>> 0 || 1;
	return () => {
		state ^= state << 13;
		state ^= state >>> 17;
		state ^= state << 5;
		return ((state >>> 0) / 0xffffffff) * 2 - 1;
	};
}

export interface StereoNoiseArrays {
	left: Float32Array;
	right: Float32Array;
	sampleRate: number;
}

/** Build uncorrelated L/R white noise arrays for one deck. */
export function buildWhiteNoiseArrays(
	sampleRate: number,
	seconds = NOISE_LOOP_SECONDS,
	deck: DeckId = 1
): StereoNoiseArrays {
	const seed = deckNoiseSeed(deck);
	const length = Math.max(1, Math.round(sampleRate * seconds));
	const left = new Float32Array(length);
	const right = new Float32Array(length);
	const nextL = seededUnitRand(seed * 2);
	const nextR = seededUnitRand(seed * 2 + 1);
	for (let i = 0; i < length; i++) {
		left[i] = nextL();
		right[i] = nextR();
	}
	return { left, right, sampleRate };
}

interface PinkState {
	b0: number;
	b1: number;
	b2: number;
	b3: number;
	b4: number;
	b5: number;
	b6: number;
}

function _pinkStep(white: number, st: PinkState): number {
	st.b0 = 0.99886 * st.b0 + white * 0.0555179;
	st.b1 = 0.99332 * st.b1 + white * 0.0750759;
	st.b2 = 0.969 * st.b2 + white * 0.153852;
	st.b3 = 0.8665 * st.b3 + white * 0.3104856;
	st.b4 = 0.55 * st.b4 + white * 0.5329522;
	st.b5 = -0.7616 * st.b5 - white * 0.016898;
	const pink = st.b0 + st.b1 + st.b2 + st.b3 + st.b4 + st.b5 + st.b6 + white * 0.5362;
	st.b6 = white * 0.115926;
	return pink * 0.11;
}

/** Paul Kellet pinking: -3 dB/oct from the same seeded white stream. */
export function pinkFromWhite(arrays: StereoNoiseArrays): StereoNoiseArrays {
	const left = new Float32Array(arrays.left.length);
	const right = new Float32Array(arrays.right.length);
	const stL: PinkState = { b0: 0, b1: 0, b2: 0, b3: 0, b4: 0, b5: 0, b6: 0 };
	const stR: PinkState = { b0: 0, b1: 0, b2: 0, b3: 0, b4: 0, b5: 0, b6: 0 };
	for (let i = 0; i < arrays.left.length; i++) {
		left[i] = _pinkStep(arrays.left[i], stL);
		right[i] = _pinkStep(arrays.right[i], stR);
	}
	return { left, right, sampleRate: arrays.sampleRate };
}

function rms(arr: Float32Array): number {
	let sum = 0;
	for (let i = 0; i < arr.length; i++) sum += arr[i] * arr[i];
	return Math.sqrt(sum / Math.max(1, arr.length));
}

/** Match pink loudness to white so full-knob travel aligns (#10270). */
export function levelMatchPinkToWhite(
	white: StereoNoiseArrays,
	pink: StereoNoiseArrays
): StereoNoiseArrays {
	const w = Math.sqrt((rms(white.left) ** 2 + rms(white.right) ** 2) / 2) || 1;
	const p = Math.sqrt((rms(pink.left) ** 2 + rms(pink.right) ** 2) / 2) || 1;
	const gain = w / p;
	const left = new Float32Array(pink.left.length);
	const right = new Float32Array(pink.right.length);
	for (let i = 0; i < pink.left.length; i++) {
		left[i] = pink.left[i] * gain;
		right[i] = pink.right[i] * gain;
	}
	return { left, right, sampleRate: pink.sampleRate };
}

export function buildNoiseArrays(
	sampleRate: number,
	colour: NoiseColour,
	deck: DeckId = 1
): StereoNoiseArrays {
	const white = buildWhiteNoiseArrays(sampleRate, NOISE_LOOP_SECONDS, deck);
	if (colour === 'white') return white;
	return levelMatchPinkToWhite(white, pinkFromWhite(white));
}

export function createLoopingNoiseBuffer(
	ctx: BaseAudioContext,
	colour: NoiseColour,
	deck: DeckId
): AudioBuffer {
	const { left, right, sampleRate } = buildNoiseArrays(ctx.sampleRate, colour, deck);
	const buffer = ctx.createBuffer(2, left.length, sampleRate);
	buffer.copyToChannel(Float32Array.from(left), 0);
	buffer.copyToChannel(Float32Array.from(right), 1);
	return buffer;
}

export interface NoiseBufferNodes {
	source: AudioBufferSourceNode | null;
	level: GainNode;
}

/** Start or replace the looping noise source on an existing level gain (kernel splice). */
export function attachNoiseBufferSource(
	ctx: AudioContext,
	nodes: NoiseBufferNodes,
	buffer: AudioBuffer,
	when = ctx.currentTime
): void {
	if (nodes.source !== null) {
		try {
			nodes.source.stop(when);
		} catch {
			/* already stopped */
		}
		nodes.source.disconnect();
	}
	const source = ctx.createBufferSource();
	source.buffer = buffer;
	source.loop = true;
	source.connect(nodes.level);
	source.start(when);
	nodes.source = source;
}

/** Crossfade the kernel level while swapping buffers (variant change or rebuild). */
export function swapNoiseBufferWithRamp(
	ctx: AudioContext,
	nodes: NoiseBufferNodes,
	newBuffer: AudioBuffer,
	rampSec = NOISE_BUFFER_RAMP_SEC
): void {
	const when = ctx.currentTime;
	const end = when + rampSec;
	const peakBefore = nodes.level.gain.value;
	nodes.level.gain.cancelScheduledValues(when);
	nodes.level.gain.setValueAtTime(peakBefore, when);
	nodes.level.gain.linearRampToValueAtTime(0, end);
	attachNoiseBufferSource(ctx, nodes, newBuffer, end);
	nodes.level.gain.setValueAtTime(0, end);
	nodes.level.gain.linearRampToValueAtTime(peakBefore, end + rampSec);
}

export function noiseGainAtFullTravel(parameter = 1): number {
	const halfPi = Math.PI / 2;
	const wet = Math.sin(halfPi * parameter);
	return wet * 10 ** (NOISE_GAIN_MAX_DB / 20);
}

export type NoiseColourChangeHandler = (colour: NoiseColour) => void;

export function bindNoiseColourVariant(handler: NoiseColourChangeHandler): () => void {
	return onVariantChange(NOISE_COLOUR_VARIANT.id, (_id, _from, to) => {
		if (to === 'white' || to === 'pink') handler(to);
	});
}
