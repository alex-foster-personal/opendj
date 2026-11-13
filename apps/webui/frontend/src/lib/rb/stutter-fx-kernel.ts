/**
 * Beat FX STUTTER DSP core (issue #10226): capture one bar at the deck tempo, then
 * replay a rotated chunk at a beat length with optional reverse, resync and blend.
 * Traktor Beatmasher 2 is the market model; unlike ROLL (#9793) the whole bar stays
 * in memory so any 1/8-aligned chunk can be juggled.
 *
 * Block host only: callers invoke `render()` per 128-frame quantum. Zero added
 * latency (output frame n uses input ≤ n). One stereo bar buffer is allocated on
 * engage; `render()` allocates nothing. Worklet registration is #10234; CPU row #9777.
 */
import { onVariantChange } from './variant-prefs';
import { STUTTER_CHUNK_VARIANT, STUTTER_BLEND_VARIANT } from './stutter-fx-variants';

export { STUTTER_CHUNK_VARIANT, STUTTER_BLEND_VARIANT };

export const STUTTER_BAR_BEATS = 4;
export const STUTTER_LENGTHS = ['1/8', '1/4', '1/2', '1', '2', '4'] as const;
export type StutterLength = (typeof STUTTER_LENGTHS)[number];

export const STUTTER_LENGTH_BEATS: Readonly<Record<StutterLength, number>> = Object.freeze({
	'1/8': 0.125,
	'1/4': 0.25,
	'1/2': 0.5,
	'1': 1,
	'2': 2,
	'4': 4
});

export const STUTTER_ROTATE_MAX = 7;
export const STUTTER_XFADE_CANDIDATES = [128, 256, 512] as const;
/** Sweep test 6 (Mon 9 Nov 2026): shortest X with all edge veto ratios ≤ 1.5. */
export const STUTTER_EDGE_XFADE_FRAMES: 128 | 256 | 512 = 128;
export const STUTTER_RENDER_QUANTUM = 128;

const SUBNORMAL_FLUSH = 1.1754943508222875e-38;

export type StutterBlendLaw = 'equal-power' | 'dry-held';

export function stutterBlendGains(law: StutterBlendLaw, depth: number): [number, number] {
	if (depth <= 0) return [1, 0];
	if (depth >= 1) return [0, 1];
	if (law === 'equal-power') {
		const angle = depth * (Math.PI / 2);
		return [Math.cos(angle), Math.sin(angle)];
	}
	return [Math.min(1, 2 * (1 - depth)), Math.min(1, 2 * depth)];
}

export function parseStutterLength(v: unknown): StutterLength {
	if (typeof v === 'string' && (STUTTER_LENGTHS as readonly string[]).includes(v)) {
		return v as StutterLength;
	}
	throw new RangeError(
		`stutter length ${JSON.stringify(v)} is not one of ${STUTTER_LENGTHS.join(', ')}`
	);
}

export interface StutterFxKernelOptions {
	sampleRate: number;
	deck: string;
	beatSeconds: () => number | null;
	readVariant(id: string): string;
	edgeXfadeFrames?: 128 | 256 | 512;
}

export interface StutterFxKernel {
	setEngaged(on: boolean): void;
	setLength(len: StutterLength): void;
	setRotate(steps: number): void;
	setReverse(on: boolean): void;
	resync(): void;
	setDepth(x: number, at?: number): void;
	setTiming(seconds: number, at?: number): void;
	render(inL: Float32Array, inR: Float32Array, outL: Float32Array, outR: Float32Array): void;
	readonly latencyFrames: 0;
	readonly state: 'off' | 'capturing' | 'playing' | 'releasing';
	readonly bufferFrames: number;
	readonly allocations: number;
	dispose(): void;
}

function assertFiniteDepth(x: number): void {
	if (!Number.isFinite(x) || x < 0 || x > 1) {
		throw new RangeError(`depth must be finite in [0, 1], got ${x}`);
	}
}

function assertRotate(steps: number): void {
	if (!Number.isInteger(steps) || steps < 0 || steps > STUTTER_ROTATE_MAX) {
		throw new RangeError(`rotate must be an integer 0..${STUTTER_ROTATE_MAX}, got ${steps}`);
	}
}

function flushSubnormal(v: number): number {
	return Math.abs(v) < SUBNORMAL_FLUSH ? 0 : v;
}

function wrapPos(pos: number, len: number): number {
	let p = pos % len;
	if (p < 0) p += len;
	return p;
}

function readInterp(buf: Float32Array, pos: number, barLen: number): number {
	const p = wrapPos(pos, barLen);
	const i0 = Math.floor(p);
	const frac = p - i0;
	const i1 = (i0 + 1) % barLen;
	return buf[i0] * (1 - frac) + buf[i1] * frac;
}

function originForRotate(r: number, bFrames: number): number {
	return (r * bFrames) / 2;
}

type KernelState = 'off' | 'capturing' | 'playing' | 'releasing';

export function createStutterFxKernel(opts: StutterFxKernelOptions): StutterFxKernel {
	const sr = opts.sampleRate;
	const deck = opts.deck;
	const edgeX = opts.edgeXfadeFrames ?? STUTTER_EDGE_XFADE_FRAMES;
	const beatSeconds = opts.beatSeconds;
	const readVariant = opts.readVariant;

	const wet = { l: 0, r: 0 };
	const gains = { dry: 0, wet: 0 };
	const gTmp = { dry: 0, wet: 0 };

	let disposed = false;
	let engaged = false;
	let pendingEngage = false;
	let state: KernelState = 'off';

	let beatFrames = 0;
	let barFrames = 0;
	let bufL: Float32Array | null = null;
	let bufR: Float32Array | null = null;
	let allocations = 0;

	let capWrite = 0;

	let lenId: StutterLength = parseStutterLength(readVariant(STUTTER_CHUNK_VARIANT.id));
	let explicitLength = false;
	let pendingLen: StutterLength | null = null;

	let rotate = 0;
	let pendingRotate: number | null = null;

	let reverse = false;
	let revXfadeLeft = 0;
	let revXfadeFromReverse = false;

	let resyncArmed = false;
	let resyncPhase: 'none' | 'fade-dry' | 're-capture' = 'none';
	let resyncFadeLeft = 0;
	let resyncFadeFromDry = 1;
	let resyncFadeFromWet = 0;

	const blendInit = readVariant(STUTTER_BLEND_VARIANT.id);
	if (blendInit !== 'equal-power' && blendInit !== 'dry-held') {
		throw new RangeError(`unknown stutter blend variant ${JSON.stringify(blendInit)}`);
	}
	let blendLaw: StutterBlendLaw = blendInit;
	let blendXfadeLeft = 0;
	let blendXfadeFromDry = 1;
	let blendXfadeFromWet = 0;
	let blendXfadeToDry = 1;
	let blendXfadeToWet = 0;

	let depth = 0;
	let depthTarget = 0;
	let depthRampLeft = 0;
	let depthRampFromDry = 1;
	let depthRampFromWet = 0;
	let depthRampToDry = 1;
	let depthRampToWet = 0;

	let playBlendXfadeLeft = 0;

	let chunkXfadeLeft = 0;
	let chunkXfadeTotal = 0;
	let chunkXfadeTailOff = 0;
	let chunkXfadeHeadOff = 0;
	let chunkXfadeFromLenF = 0;
	let chunkXfadeToLenF = 0;
	let chunkXfadeFromOrigin = 0;
	let chunkXfadeToOrigin = 0;
	let chunkXfadeFromRev = false;
	let chunkXfadeToRev = false;

	let releaseXfadeLeft = 0;

	let playFrame = 0;
	let chunkPhase = 0;

	function lenFramesNow(): number {
		return STUTTER_LENGTH_BEATS[lenId] * beatFrames;
	}

	function fillBlendGains(
		law: StutterBlendLaw,
		d: number,
		target: { dry: number; wet: number }
	): void {
		if (d <= 0) {
			target.dry = 1;
			target.wet = 0;
			return;
		}
		if (d >= 1) {
			target.dry = 0;
			target.wet = 1;
			return;
		}
		if (law === 'equal-power') {
			const angle = d * (Math.PI / 2);
			target.dry = Math.cos(angle);
			target.wet = Math.sin(angle);
			return;
		}
		target.dry = Math.min(1, 2 * (1 - d));
		target.wet = Math.min(1, 2 * d);
	}

	function wetAtContinuous(off: number, lenF: number, rev: boolean, origin: number): void {
		const barLen = barFrames;
		const bL = bufL!;
		const bR = bufR!;
		const readOff = rev ? origin + lenF - 1 - off : origin + off;
		wet.l = readInterp(bL, readOff, barLen);
		wet.r = readInterp(bR, readOff, barLen);
	}

	function applyGains(
		i: number,
		inL: Float32Array,
		inR: Float32Array,
		outL: Float32Array,
		outR: Float32Array,
		wL: number,
		wR: number,
		dryG: number,
		wetG: number
	): void {
		if (dryG === 1 && wetG === 0) {
			outL[i] = inL[i];
			outR[i] = inR[i];
			return;
		}
		if (dryG === 0 && wetG === 1) {
			outL[i] = flushSubnormal(wL);
			outR[i] = flushSubnormal(wR);
			return;
		}
		outL[i] = dryG * inL[i] + wetG * flushSubnormal(wL);
		outR[i] = dryG * inR[i] + wetG * flushSubnormal(wR);
	}

	function startDepthRamp(): void {
		depthRampLeft = STUTTER_RENDER_QUANTUM;
		depthRampFromDry = depthRampToDry;
		depthRampFromWet = depthRampToWet;
		fillBlendGains(blendLaw, depthTarget, gTmp);
		depthRampToDry = gTmp.dry;
		depthRampToWet = gTmp.wet;
	}

	function gainsAtFrame(): void {
		fillBlendGains(blendLaw, depthTarget, gTmp);
		gains.dry = gTmp.dry;
		gains.wet = gTmp.wet;

		if (resyncPhase === 'fade-dry' && resyncFadeLeft > 0) {
			const t = resyncFadeLeft / edgeX;
			gains.dry = resyncFadeFromDry * t + (1 - t);
			gains.wet = resyncFadeFromWet * t;
			return;
		}
		if (state === 'releasing' && releaseXfadeLeft > 0) {
			const t = releaseXfadeLeft / edgeX;
			gains.dry = gTmp.dry * t + (1 - t);
			gains.wet = gTmp.wet * t;
			return;
		}
		if (playBlendXfadeLeft > 0) {
			const t = 1 - playBlendXfadeLeft / edgeX;
			gains.dry = 1 * (1 - t) + gTmp.dry * t;
			gains.wet = gTmp.wet * t;
			return;
		}
		if (blendXfadeLeft > 0) {
			const t = 1 - blendXfadeLeft / edgeX;
			gains.dry = blendXfadeFromDry * (1 - t) + t * blendXfadeToDry;
			gains.wet = blendXfadeFromWet * (1 - t) + t * blendXfadeToWet;
			return;
		}
		if (depthRampLeft > 0) {
			const t = 1 - depthRampLeft / STUTTER_RENDER_QUANTUM;
			gains.dry = depthRampFromDry * (1 - t) + t * depthRampToDry;
			gains.wet = depthRampFromWet * (1 - t) + t * depthRampToWet;
		}
	}

	function latchChunkParams(): void {
		if (pendingLen !== null) {
			lenId = pendingLen;
			pendingLen = null;
		}
		if (pendingRotate !== null) {
			rotate = pendingRotate;
			pendingRotate = null;
		}
	}

	function ensureBarCapacity(): void {
		const bs = beatSeconds();
		if (bs === null || !Number.isFinite(bs) || bs <= 0) return;
		beatFrames = bs * sr;
		const newBar = Math.round(STUTTER_BAR_BEATS * beatFrames);
		if (newBar <= barFrames) {
			if (bufL) bufL.fill(0);
			if (bufR) bufR.fill(0);
			return;
		}
		bufL = new Float32Array(newBar);
		bufR = new Float32Array(newBar);
		allocations += 1;
		barFrames = newBar;
	}

	function startChunkEdgeXfade(fromLenF: number, fromOrigin: number, fromRev: boolean): void {
		chunkXfadeFromLenF = fromLenF;
		chunkXfadeFromOrigin = fromOrigin;
		chunkXfadeFromRev = fromRev;
		latchChunkParams();
		chunkXfadeToLenF = lenFramesNow();
		chunkXfadeToOrigin = originForRotate(rotate, beatFrames);
		chunkXfadeToRev = reverse;
		const xLen = Math.min(edgeX, Math.floor(Math.min(fromLenF, chunkXfadeToLenF) / 2));
		chunkXfadeLeft = xLen;
		chunkXfadeTotal = xLen;
		chunkXfadeTailOff = fromLenF - 1;
		chunkXfadeHeadOff = 0;
	}

	function beginEngage(): void {
		const bs = beatSeconds();
		if (bs === null || !Number.isFinite(bs) || bs <= 0) {
			throw new Error(
				`STUTTER on deck ${deck}: no tempo source (beatSeconds returned ${String(bs)})`
			);
		}
		beatFrames = bs * sr;
		barFrames = Math.round(STUTTER_BAR_BEATS * beatFrames);
		if (!bufL || bufL.length < barFrames) {
			bufL = new Float32Array(barFrames);
			bufR = new Float32Array(barFrames);
			allocations += 1;
		} else if (bufL && bufR) {
			bufL.fill(0);
			bufR.fill(0);
		}
		capWrite = 0;
		playFrame = 0;
		chunkPhase = 0;
		state = 'capturing';
		resyncPhase = 'none';
		resyncFadeLeft = 0;
		lenId = explicitLength ? lenId : parseStutterLength(readVariant(STUTTER_CHUNK_VARIANT.id));
	}

	const unsubChunk = onVariantChange(STUTTER_CHUNK_VARIANT.id, () => {
		if (disposed || explicitLength) return;
		pendingLen = parseStutterLength(readVariant(STUTTER_CHUNK_VARIANT.id));
	});

	const unsubBlend = onVariantChange(STUTTER_BLEND_VARIANT.id, (_id, _from, to) => {
		if (disposed) return;
		const next = to as StutterBlendLaw;
		if (next !== 'equal-power' && next !== 'dry-held') return;
		fillBlendGains(blendLaw, depth, gTmp);
		blendXfadeFromDry = gTmp.dry;
		blendXfadeFromWet = gTmp.wet;
		blendLaw = next;
		fillBlendGains(blendLaw, depth, gTmp);
		blendXfadeToDry = gTmp.dry;
		blendXfadeToWet = gTmp.wet;
		blendXfadeLeft = edgeX;
	});

	const kernel: StutterFxKernel = {
		get latencyFrames(): 0 {
			return 0;
		},
		get state(): KernelState {
			return state;
		},
		get bufferFrames(): number {
			return barFrames;
		},
		get allocations(): number {
			return allocations;
		},

		setEngaged(on: boolean): void {
			if (disposed) throw new Error('stutter fx kernel disposed');
			if (on === engaged) return;
			engaged = on;
			if (on) {
				pendingEngage = true;
			} else if (state === 'capturing') {
				state = 'off';
				capWrite = 0;
				playFrame = 0;
				chunkPhase = 0;
				pendingEngage = false;
			} else if (state === 'playing') {
				state = 'releasing';
				releaseXfadeLeft = edgeX;
				fillBlendGains(blendLaw, depth, gTmp);
				blendXfadeFromDry = gTmp.dry;
				blendXfadeFromWet = gTmp.wet;
			} else {
				state = 'off';
			}
		},

		setLength(len: StutterLength): void {
			if (disposed) throw new Error('stutter fx kernel disposed');
			parseStutterLength(len);
			explicitLength = true;
			pendingLen = len;
		},

		setRotate(steps: number): void {
			if (disposed) throw new Error('stutter fx kernel disposed');
			assertRotate(steps);
			pendingRotate = steps;
		},

		setReverse(on: boolean): void {
			if (disposed) throw new Error('stutter fx kernel disposed');
			if (on === reverse) return;
			const lenF = lenFramesNow();
			revXfadeLeft = Math.min(edgeX, Math.floor(lenF / 2));
			revXfadeFromReverse = reverse;
			reverse = on;
		},

		resync(): void {
			if (disposed) throw new Error('stutter fx kernel disposed');
			if (!engaged || state === 'off') return;
			resyncArmed = true;
		},

		setDepth(x: number, _at?: number): void {
			if (disposed) throw new Error('stutter fx kernel disposed');
			assertFiniteDepth(x);
			if (x === depthTarget) return;
			depthTarget = x;
			startDepthRamp();
		},

		setTiming(seconds: number, _at?: number): void {
			if (disposed) throw new Error('stutter fx kernel disposed');
			if (!Number.isFinite(seconds) || seconds < 0) {
				throw new RangeError(`setTiming seconds must be finite and >= 0, got ${seconds}`);
			}
		},

		render(inL: Float32Array, inR: Float32Array, outL: Float32Array, outR: Float32Array): void {
			if (disposed) throw new Error('stutter fx kernel disposed');
			const n = inL.length;
			if (inR.length !== n || outL.length !== n || outR.length !== n) {
				throw new RangeError('stutter render: channel lengths must match');
			}
			if (n > STUTTER_RENDER_QUANTUM) {
				throw new RangeError(`stutter render: at most ${STUTTER_RENDER_QUANTUM} frames`);
			}

			if (pendingEngage) {
				beginEngage();
				pendingEngage = false;
			}

			if (!engaged && state === 'off') {
				if (n === inL.length) {
					outL.set(inL);
					outR.set(inR);
				}
				return;
			}

			for (let i = 0; i < n; i += 1) {
				if (releaseXfadeLeft > 0) releaseXfadeLeft -= 1;
				if (resyncFadeLeft > 0) resyncFadeLeft -= 1;
				if (playBlendXfadeLeft > 0) playBlendXfadeLeft -= 1;
				if (blendXfadeLeft > 0) blendXfadeLeft -= 1;
				if (depthRampLeft > 0) {
					depthRampLeft -= 1;
					if (depthRampLeft === 0) depth = depthTarget;
				}

				if (resyncArmed && state === 'playing' && playFrame > 0 && barFrames > 0) {
					const barPos = playFrame % barFrames;
					const toBar = barPos === 0 ? barFrames : barFrames - barPos;
					if (toBar === edgeX) {
						resyncArmed = false;
						resyncPhase = 'fade-dry';
						resyncFadeLeft = edgeX;
						fillBlendGains(blendLaw, depth, gTmp);
						resyncFadeFromDry = gTmp.dry;
						resyncFadeFromWet = gTmp.wet;
					}
				}

				if (resyncPhase === 'fade-dry' && resyncFadeLeft === 0 && playFrame % barFrames === 0) {
					resyncPhase = 're-capture';
					state = 'capturing';
					capWrite = 0;
					chunkPhase = 0;
					ensureBarCapacity();
				}

				if (state === 'capturing') {
					if (capWrite < barFrames) {
						bufL![capWrite] = inL[i];
						bufR![capWrite] = inR[i];
						capWrite += 1;
						outL[i] = inL[i];
						outR[i] = inR[i];
						if (capWrite < barFrames) continue;
						if (resyncPhase === 're-capture') resyncPhase = 'none';
						state = 'playing';
						playFrame = 0;
						chunkPhase = 0;
						latchChunkParams();
						playBlendXfadeLeft = edgeX;
					} else {
						outL[i] = inL[i];
						outR[i] = inR[i];
						continue;
					}
				}

				if (state === 'releasing') {
					if (releaseXfadeLeft === 0) {
						state = 'off';
						engaged = false;
						outL[i] = inL[i];
						outR[i] = inR[i];
						continue;
					}
					gainsAtFrame();
					let wL = inL[i];
					let wR = inR[i];
					if (gains.wet > 0 && bufL) {
						const lenF = lenFramesNow();
						const origin = originForRotate(rotate, beatFrames);
						wetAtContinuous(chunkPhase, lenF, reverse, origin);
						wL = wet.l;
						wR = wet.r;
					}
					applyGains(i, inL, inR, outL, outR, wL, wR, gains.dry, gains.wet);
					continue;
				}

				if (state === 'playing' && bufL) {
					const lenF = lenFramesNow();
					const xLen = Math.min(edgeX, Math.floor(lenF / 2));
					const origin = originForRotate(rotate, beatFrames);

					let wL: number;
					let wR: number;

					if (chunkXfadeLeft > 0 && chunkXfadeTotal > 0) {
						const stepped = chunkXfadeTotal - chunkXfadeLeft + 1;
						const t = stepped / chunkXfadeTotal;
						const tailOff = wrapPos(chunkXfadeTailOff + stepped - 1, chunkXfadeFromLenF);
						const headOff = wrapPos(chunkXfadeHeadOff + stepped - 1, chunkXfadeToLenF);
						wetAtContinuous(tailOff, chunkXfadeFromLenF, chunkXfadeFromRev, chunkXfadeFromOrigin);
						const fromL = wet.l;
						const fromR = wet.r;
						wetAtContinuous(headOff, chunkXfadeToLenF, chunkXfadeToRev, chunkXfadeToOrigin);
						wL = fromL * (1 - t) + wet.l * t;
						wR = fromR * (1 - t) + wet.r * t;
						chunkXfadeLeft -= 1;
					} else if (revXfadeLeft > 0) {
						const revTotal = Math.min(edgeX, Math.floor(lenF / 2));
						const stepped = revTotal - revXfadeLeft + 1;
						const t = stepped / revTotal;
						wetAtContinuous(chunkPhase, lenF, revXfadeFromReverse, origin);
						const fromL = wet.l;
						const fromR = wet.r;
						wetAtContinuous(chunkPhase, lenF, reverse, origin);
						wL = fromL * (1 - t) + wet.l * t;
						wR = fromR * (1 - t) + wet.r * t;
						revXfadeLeft -= 1;
					} else {
						wetAtContinuous(chunkPhase, lenF, reverse, origin);
						wL = wet.l;
						wR = wet.r;
					}

					gainsAtFrame();
					applyGains(i, inL, inR, outL, outR, wL, wR, gains.dry, gains.wet);

					if (state !== 'playing') {
						continue;
					}

					playFrame += 1;
					chunkPhase += 1;
					if (chunkXfadeTotal > 0 && chunkXfadeLeft > 0) {
						continue;
					}
					if (chunkXfadeTotal > 0 && chunkXfadeLeft === 0) {
						chunkXfadeTotal = 0;
						continue;
					}
					if (chunkPhase >= lenF) {
						chunkPhase -= lenF;
						startChunkEdgeXfade(lenF, origin, reverse);
					}
					continue;
				}

				outL[i] = inL[i];
				outR[i] = inR[i];
			}
		},

		dispose(): void {
			if (disposed) return;
			disposed = true;
			unsubChunk();
			unsubBlend();
			bufL = null;
			bufR = null;
		}
	};

	return kernel;
}
