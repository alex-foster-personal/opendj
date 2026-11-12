/**
 * Beat FX ROLL DSP core (issue #9793): records the input from ON for one beat division,
 * repeats it with a seam crossfade, blends dry and roll via LEVEL/DEPTH.
 *
 * Spec: `(private path)` sections 3-4.
 * Mimicked: Pioneer DJM-900NXS2 Operating Instructions pp. 16-17; rekordbox 7.2.8 manual
 * pp. 177-178 (LEVEL/DEPTH endpoints).
 *
 * Block host only: callers invoke `render()` per 128-frame quantum; zero added latency;
 * one stereo buffer allocated at ON (or when a longer division needs it); `render()`
 * allocates nothing. Host registration is #9794; panel is family 01; harness is #9774.
 */
import { BEAT_FX_MAX_BPM, BEAT_FX_MIN_BPM } from './beat-fx';

/** Effect name; Rust engine mode refuses it by this name. */
export const ROLL_FX_EFFECT = 'roll' as const;
export const ROLL_FX_LABEL = 'ROLL';
export const ROLL_FX_DIVISIONS = [1 / 16, 1 / 8, 1 / 4, 1 / 2, 1, 2, 4, 8, 16] as const;
export type RollFxDivision = (typeof ROLL_FX_DIVISIONS)[number];
export const ROLL_FX_DEFAULT_DIVISION: RollFxDivision = 1 / 4;

/** hold-3db from export VARIANTS is calibration-only (#9846); not implemented here. */
export const ROLL_FX_LEVEL_LAWS = ['equal-gain', 'equal-power'] as const;
export type RollFxLevelLaw = (typeof ROLL_FX_LEVEL_LAWS)[number];
/** Anchor until roll-slip-8 calibration (#9846) locks it; matches ANCHOR level-law in export script. */
export const ROLL_FX_DEFAULT_LEVEL_LAW: RollFxLevelLaw = 'equal-power';
export const ROLL_FX_DEFAULT_DEPTH = 0.5;

export const ROLL_FX_XFADE_CANDIDATES = [128, 256, 512] as const;
/** Shortest candidate that meets the click-ratio 1.5 veto on the test fixtures. */
export const ROLL_FX_XFADE_FRAMES: 128 | 256 | 512 = 128;
export const ROLL_FX_RENDER_QUANTUM = 128;
export const ROLL_FX_MAX_LATENCY_FRAMES = 128;

const SUBNORMAL_FLUSH = 1.1754943508222875e-38;

export function rollLevelGains(law: RollFxLevelLaw, depth: number): [number, number] {
	if (!Number.isFinite(depth)) {
		throw new RangeError(`depth must be finite, got ${depth}`);
	}
	if (depth <= 0) return [1, 0];
	if (depth >= 1) return [0, 1];
	if (law === 'equal-power') {
		const angle = depth * (Math.PI / 2);
		return [Math.cos(angle), Math.sin(angle)];
	}
	return [1 - depth, depth];
}

export function parseRollDivision(v: unknown): RollFxDivision {
	for (let i = 0; i < ROLL_FX_DIVISIONS.length; i += 1) {
		if (v === ROLL_FX_DIVISIONS[i]) return ROLL_FX_DIVISIONS[i];
	}
	throw new RangeError(
		`roll division ${String(v)} is not one of 1/16, 1/8, 1/4, 1/2, 1, 2, 4, 8, 16 beats`
	);
}

export function rollFxRustRefusal(deck: string): string {
	return `RUST_ENGINE_UNAVAILABLE: beat FX ROLL (${ROLL_FX_EFFECT}) on deck ${deck} plays in Web Audio only - see PARITY-TODO`;
}

export interface RollFxKernelOptions {
	sampleRate: number;
	deck: string;
	beatSeconds: (() => number | null) | null | undefined;
	levelLaw?: RollFxLevelLaw;
	xfadeFrames?: 128 | 256 | 512;
	division?: RollFxDivision;
	depth?: number;
	engine?: 'web-audio' | 'rust';
}

export type RollFxState = 'off' | 'capturing' | 'playing' | 'releasing';

export interface RollFxKernel {
	setEngaged(on: boolean): void;
	setDivision(beats: RollFxDivision): void;
	setDepth(x: number): void;
	render(inL: Float32Array, inR: Float32Array, outL: Float32Array, outR: Float32Array): void;
	readonly latencyFrames: 0;
	readonly state: RollFxState;
	readonly division: RollFxDivision;
	readonly repeatFrames: number;
	readonly bufferFrames: number;
	readonly allocations: number;
	dispose(): void;
}

function assertDepth(x: number): void {
	if (!Number.isFinite(x) || x < 0 || x > 1) {
		throw new RangeError(`depth must be finite in [0, 1], got ${x}`);
	}
}

function assertLevelLaw(law: unknown): RollFxLevelLaw {
	if (law === 'equal-gain' || law === 'equal-power') return law;
	throw new RangeError(`levelLaw must be equal-gain or equal-power, got ${String(law)}`);
}

function assertXfade(x: unknown): 128 | 256 | 512 {
	if (x === 128 || x === 256 || x === 512) return x;
	throw new RangeError(`xfadeFrames must be 128, 256 or 512, got ${String(x)}`);
}

export function createRollFxKernel(opts: RollFxKernelOptions): RollFxKernel {
	const deck = opts.deck;
	const engine = opts.engine ?? 'web-audio';
	if (engine === 'rust') {
		throw new Error(rollFxRustRefusal(deck));
	}
	if (typeof deck !== 'string' || deck.length === 0) {
		throw new RangeError('deck must be a non-empty string');
	}
	const sampleRate = opts.sampleRate;
	if (!Number.isFinite(sampleRate) || sampleRate <= 0) {
		throw new RangeError(`sampleRate must be finite and > 0, got ${sampleRate}`);
	}
	const beatSecondsRaw = opts.beatSeconds;
	if (typeof beatSecondsRaw !== 'function') {
		throw new Error(`ROLL on deck ${deck}: no tempo source`);
	}
	const beatSecondsFn: () => number | null = beatSecondsRaw;
	const levelLaw = assertLevelLaw(opts.levelLaw ?? ROLL_FX_DEFAULT_LEVEL_LAW);
	const xfadeFrames = assertXfade(opts.xfadeFrames ?? ROLL_FX_XFADE_FRAMES);
	let division = parseRollDivision(opts.division ?? ROLL_FX_DEFAULT_DIVISION);
	assertDepth(opts.depth ?? ROLL_FX_DEFAULT_DEPTH);
	let depth = opts.depth ?? ROLL_FX_DEFAULT_DEPTH;
	let depthTarget = depth;

	let disposed = false;
	let state: RollFxState = 'off';
	let bufL: Float32Array | null = null;
	let bufR: Float32Array | null = null;
	let allocations = 0;
	let repeatFrames = 0;
	let bufferFrames = 0;
	let capTailTarget = 0;
	let capPos = 0;
	let phase = 0;
	let captureTail = false;

	let gd = 1;
	let gw = 0;
	let depthRampLeft = 0;
	let depthRampFromGd = 1;
	let depthRampFromGw = 0;
	let depthRampToGd = 1;
	let depthRampToGw = 0;

	let entryXfadeLeft = 0;

	let releaseLeft = 0;
	let releaseReason: 'off' | 'division' | 'tempo' = 'off';
	let releasePhase = 0;
	let pendingCapture = false;
	let pendingDiv: RollFxDivision | null = null;
	let pendingBeatSec = 0;
	let pendingBufL: Float32Array | null = null;
	let pendingBufR: Float32Array | null = null;

	function readBeatSeconds(): number {
		const bs = beatSecondsFn();
		if (typeof bs !== 'number' || !Number.isFinite(bs) || bs <= 0) {
			throw new Error(`ROLL on deck ${deck}: no tempo source (beatSeconds returned ${String(bs)})`);
		}
		const bpm = 60 / bs;
		if (bpm < BEAT_FX_MIN_BPM || bpm > BEAT_FX_MAX_BPM) {
			throw new Error(`ROLL on deck ${deck}: no tempo source (beatSeconds returned ${String(bs)})`);
		}
		return bs;
	}

	function computeRepeatFrames(bs: number, div: RollFxDivision): number {
		return div * bs * sampleRate;
	}

	function ensureBuffer(bs: number, div: RollFxDivision, extraPair: boolean): void {
		const L = computeRepeatFrames(bs, div);
		const need = Math.ceil(L) + xfadeFrames + 2;
		if (!bufL || bufL.length < need) {
			bufL = new Float32Array(need);
			bufR = new Float32Array(need);
			allocations += 1;
		} else if (extraPair) {
			const nL = new Float32Array(need);
			const nR = new Float32Array(need);
			pendingBufL = nL;
			pendingBufR = nR;
			allocations += 1;
		}
		repeatFrames = L;
		bufferFrames = need;
		capTailTarget = need;
	}

	function fillGains(d: number): void {
		const pair = rollLevelGains(levelLaw, d);
		gd = pair[0];
		gw = pair[1];
	}

	function startDepthRamp(): void {
		depthRampFromGd = gd;
		depthRampFromGw = gw;
		const target = rollLevelGains(levelLaw, depthTarget);
		depthRampToGd = target[0];
		depthRampToGw = target[1];
		depthRampLeft = ROLL_FX_RENDER_QUANTUM;
	}

	function releaseBuffers(): void {
		bufL = null;
		bufR = null;
		pendingBufL = null;
		pendingBufR = null;
		bufferFrames = 0;
		repeatFrames = 0;
	}

	function beginCapturing(bs: number, div: RollFxDivision): void {
		ensureBuffer(bs, div, false);
		state = 'capturing';
		capPos = 0;
		phase = 0;
		entryXfadeLeft = 0;
		captureTail = false;
		releaseLeft = 0;
		pendingDiv = null;
	}

	const kernel: RollFxKernel = {
		get latencyFrames(): 0 {
			return 0;
		},
		get state() {
			return state;
		},
		get division() {
			return division;
		},
		get repeatFrames() {
			return repeatFrames;
		},
		get bufferFrames() {
			return bufferFrames;
		},
		get allocations() {
			return allocations;
		},

		setEngaged(on: boolean): void {
			if (disposed) throw new Error('roll fx kernel disposed');
			if (on) {
				if (state === 'capturing' || state === 'playing') return;
				if (state === 'releasing') {
					const bs = readBeatSeconds();
					const need = Math.ceil(computeRepeatFrames(bs, division)) + xfadeFrames + 2;
					if (!bufL || bufL.length < need) {
						pendingBufL = new Float32Array(need);
						pendingBufR = new Float32Array(need);
						allocations += 1;
					}
					pendingBeatSec = bs;
					pendingCapture = true;
					return;
				}
				const bs = readBeatSeconds();
				beginCapturing(bs, division);
				fillGains(depth);
			} else {
				pendingCapture = false;
				if (state === 'off') return;
				if (state === 'capturing') {
					state = 'off';
					releaseBuffers();
					return;
				}
				if (state === 'playing') {
					releaseReason = 'off';
					releaseLeft = xfadeFrames;
					releasePhase = phase;
					state = 'releasing';
					return;
				}
				if (state === 'releasing') {
					if (releaseReason !== 'off') {
						releaseReason = 'off';
					}
					releaseLeft = xfadeFrames;
				}
			}
		},

		setDivision(beats: RollFxDivision): void {
			if (disposed) throw new Error('roll fx kernel disposed');
			const d = parseRollDivision(beats);
			if (d === division && state !== 'off') return;
			if (state === 'off') {
				division = d;
				return;
			}
			if (state === 'capturing') {
				const bs = readBeatSeconds();
				division = d;
				beginCapturing(bs, d);
				return;
			}
			if (state === 'releasing' && releaseReason === 'off') {
				division = d;
				return;
			}
			if (state === 'playing' || state === 'releasing') {
				pendingCapture = false;
				let bs: number;
				try {
					bs = readBeatSeconds();
				} catch (err) {
					releaseReason = 'tempo';
					releaseLeft = xfadeFrames;
					releasePhase = phase;
					state = 'releasing';
					pendingDiv = null;
					pendingCapture = false;
					throw err;
				}
				const Lnew = computeRepeatFrames(bs, d);
				const need = Math.ceil(Lnew) + xfadeFrames + 2;
				if (!bufL || bufL.length < need) {
					pendingBufL = new Float32Array(need);
					pendingBufR = new Float32Array(need);
					allocations += 1;
				}
				pendingDiv = d;
				pendingBeatSec = bs;
				releaseReason = 'division';
				releaseLeft = xfadeFrames;
				releasePhase = phase;
				state = 'releasing';
			}
		},

		setDepth(x: number): void {
			if (disposed) throw new Error('roll fx kernel disposed');
			assertDepth(x);
			const prevTarget = depthTarget;
			depthTarget = x;
			if (x === prevTarget && depthRampLeft === 0) return;
			startDepthRamp();
		},

		// render-path:start
		render(inL: Float32Array, inR: Float32Array, outL: Float32Array, outR: Float32Array): void {
			if (disposed) throw new Error('roll fx kernel disposed');
			const n = inL.length;
			if (inR.length !== n || outL.length !== n || outR.length !== n) {
				throw new RangeError('roll render: channel lengths must match');
			}
			if (n > ROLL_FX_RENDER_QUANTUM) {
				throw new RangeError(`roll render: at most ${ROLL_FX_RENDER_QUANTUM} frames`);
			}

			if (state === 'off') {
				outL.set(inL);
				outR.set(inR);
				return;
			}

			const L = repeatFrames;
			const X = xfadeFrames;
			const capLen = bufferFrames;

			for (let i = 0; i < n; i += 1) {
				if (depthRampLeft > 0) {
					const stepped = ROLL_FX_RENDER_QUANTUM - depthRampLeft + 1;
					const t = stepped / ROLL_FX_RENDER_QUANTUM;
					gd = depthRampFromGd + (depthRampToGd - depthRampFromGd) * t;
					gw = depthRampFromGw + (depthRampToGw - depthRampFromGw) * t;
					depthRampLeft -= 1;
					if (depthRampLeft === 0) {
						depth = depthTarget;
						gd = depthRampToGd;
						gw = depthRampToGw;
					}
				}

				if (state === 'capturing' && bufL && bufR) {
					bufL[capPos] = inL[i];
					bufR[capPos] = inR[i];
					outL[i] = inL[i];
					outR[i] = inR[i];
					capPos += 1;

					if (capPos >= L && state === 'capturing') {
						state = 'playing';
						phase = capPos - L;
						entryXfadeLeft = X;
						captureTail = true;
					}
					if (captureTail && capPos < capTailTarget) {
						continue;
					}
					if (state === 'capturing') continue;
				}

				if (state === 'releasing') {
					if (releaseLeft === 0) {
						outL[i] = inL[i];
						outR[i] = inR[i];
						if (releaseReason === 'division' && pendingDiv !== null) {
							pendingCapture = false;
							if (pendingBufL && pendingBufR) {
								bufL = pendingBufL;
								bufR = pendingBufR;
								pendingBufL = null;
								pendingBufR = null;
							}
							const div = pendingDiv;
							pendingDiv = null;
							division = div;
							repeatFrames = div * pendingBeatSec * sampleRate;
							capTailTarget = Math.ceil(repeatFrames) + xfadeFrames + 2;
							bufferFrames = capTailTarget;
							state = 'capturing';
							capPos = 0;
							phase = 0;
							entryXfadeLeft = 0;
							captureTail = true;
							fillGains(depth);
						} else if (pendingCapture) {
							pendingCapture = false;
							if (pendingBufL && pendingBufR) {
								bufL = pendingBufL;
								bufR = pendingBufR;
								pendingBufL = null;
								pendingBufR = null;
							}
							repeatFrames = division * pendingBeatSec * sampleRate;
							capTailTarget = Math.ceil(repeatFrames) + xfadeFrames + 2;
							bufferFrames = capTailTarget;
							state = 'capturing';
							capPos = 0;
							phase = 0;
							entryXfadeLeft = 0;
							captureTail = false;
							releaseLeft = 0;
							fillGains(depth);
						} else {
							state = 'off';
							releaseBuffers();
						}
						continue;
					}
					let wL = inL[i];
					let wR = inR[i];
					if (bufL && bufR && gw > 0) {
						const playPhase = releasePhase;
						const i0 = Math.floor(playPhase);
						const frac = playPhase - i0;
						const i1 = i0 + 1;
						let hL = bufL[i0];
						let hR = bufR[i0];
						if (frac > 0) {
							const bL = i1 < capLen ? bufL[i1] : bufL[0];
							const bR = i1 < capLen ? bufR[i1] : bufR[0];
							hL = bufL[i0] * (1 - frac) + bL * frac;
							hR = bufR[i0] * (1 - frac) + bR * frac;
						}
						let wetL = hL;
						let wetR = hR;
						if (playPhase < X) {
							const tailPos = playPhase + L;
							const tSeam = playPhase / X;
							const ti0 = Math.floor(tailPos);
							const tfrac = tailPos - ti0;
							const ti1 = ti0 + 1;
							const tL = bufL[ti0] * (1 - tfrac) + (ti1 < capLen ? bufL[ti1] : 0) * tfrac;
							const tR = bufR[ti0] * (1 - tfrac) + (ti1 < capLen ? bufR[ti1] : 0) * tfrac;
							wetL = tL * (1 - tSeam) + hL * tSeam;
							wetR = tR * (1 - tSeam) + hR * tSeam;
						}
						wL = gd * inL[i] + gw * wetL;
						wR = gd * inR[i] + gw * wetR;
					}

					const r = (xfadeFrames - releaseLeft) / xfadeFrames;
					const mixL = wL * (1 - r) + inL[i] * r;
					const mixR = wR * (1 - r) + inR[i] * r;
					let oL = mixL;
					let oR = mixR;
					if (Math.abs(oL) < SUBNORMAL_FLUSH) oL = 0;
					if (Math.abs(oR) < SUBNORMAL_FLUSH) oR = 0;
					outL[i] = oL;
					outR[i] = oR;

					releasePhase += 1;
					if (releasePhase >= L) releasePhase -= L;

					releaseLeft -= 1;
					continue;
				}

				if (state === 'playing' && bufL && bufR) {
					if (captureTail && capPos < capTailTarget) {
						bufL[capPos] = inL[i];
						bufR[capPos] = inR[i];
						capPos += 1;
						if (capPos >= capTailTarget) captureTail = false;
					}

					const i0 = Math.floor(phase);
					const frac = phase - i0;
					let hL = bufL[i0];
					let hR = bufR[i0];
					if (frac > 0) {
						const i1 = i0 + 1;
						const bL = i1 < capLen ? bufL[i1] : bufL[0];
						const bR = i1 < capLen ? bufR[i1] : bufR[0];
						hL = bufL[i0] * (1 - frac) + bL * frac;
						hR = bufR[i0] * (1 - frac) + bR * frac;
					}
					let wetL = hL;
					let wetR = hR;
					if (phase < X) {
						const tailPos = phase + L;
						const tSeam = phase / X;
						const ti0 = Math.floor(tailPos);
						const tfrac = tailPos - ti0;
						const ti1 = ti0 + 1;
						const tL = bufL[ti0] * (1 - tfrac) + (ti1 < capLen ? bufL[ti1] : 0) * tfrac;
						const tR = bufR[ti0] * (1 - tfrac) + (ti1 < capLen ? bufR[ti1] : 0) * tfrac;
						wetL = tL * (1 - tSeam) + hL * tSeam;
						wetR = tR * (1 - tSeam) + hR * tSeam;
					}

					phase += 1;
					if (phase >= L) phase -= L;

					const copyDry = gd === 1 && gw === 0 && entryXfadeLeft === 0 && depthRampLeft === 0;

					if (copyDry) {
						outL[i] = inL[i];
						outR[i] = inR[i];
						continue;
					}

					let mixL = gd * inL[i] + gw * wetL;
					let mixR = gd * inR[i] + gw * wetR;

					if (gw === 1 && gd === 0 && entryXfadeLeft === 0 && depthRampLeft === 0) {
						mixL = wetL;
						mixR = wetR;
					}

					let oL = mixL;
					let oR = mixR;

					if (entryXfadeLeft > 0) {
						const e = (xfadeFrames - entryXfadeLeft + 1) / xfadeFrames;
						oL = inL[i] * (1 - e) + mixL * e;
						oR = inR[i] * (1 - e) + mixR * e;
						entryXfadeLeft -= 1;
					}

					if (Math.abs(oL) < SUBNORMAL_FLUSH) oL = 0;
					if (Math.abs(oR) < SUBNORMAL_FLUSH) oR = 0;
					outL[i] = oL;
					outR[i] = oR;
					continue;
				}

				outL[i] = inL[i];
				outR[i] = inR[i];
			}
		},
		// render-path:end

		dispose(): void {
			if (disposed) return;
			disposed = true;
			state = 'off';
			releaseBuffers();
		}
	};

	fillGains(depth);
	return kernel;
}
