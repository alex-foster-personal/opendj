/**
 * Slip return crossfade (FXROLL-10595, issue #9842).
 *
 * When SLIP returns a deck to its hidden playhead (roll release, slip-loop
 * exit, SLIP off) the engine schedules a hard seek. This module blends the
 * looping (old) audio into the audio at the hidden head with a short linear
 * crossfade so the seek does not click. Lives outside audio-engine.svelte.ts so
 * that file stays within the file-size ratchet; the engine still owns the
 * schedule. Shape mimicked from Mixxx 2.5 setNewPlaypos: a linear (equal gain,
 * not equal power) crossfade on every non-loop seek.
 *
 * The blend only engages over a processor that does not crossfade its own seeks.
 * The deck's Signalsmith stretch worklet does (measured, see SLIP_RETURN_BLEND_FRAMES),
 * so on a stretch deck arm() returns 'hard:processor-crossfades' and the worklet's
 * own overlap-add is the release crossfade.
 *
 * Deliberately imports nothing heavy (no stem-graph, no stretch-adapter): the engine
 * passes the processor checks in as booleans.
 */

/** Crossfade lengths the click veto chooses between, shortest first. */
export const SLIP_RETURN_BLEND_CANDIDATES = [128, 256, 512] as const;

/** House click veto: release no sharper than 1.5x the material just before it. */
export const SLIP_RETURN_CLICK_VETO = 1.5;

/** Slip return crossfade length in frames: the shortest candidate that keeps the
 * click ratio at or under SLIP_RETURN_CLICK_VETO on every fixture x roll length.
 * Measured Mon 9 Nov 2026 by real Chromium renders of decoded audio
 * (tests/e2e/slip-return-blend-real-audio.spec.ts, raw buffer-source graph, 48 kHz,
 * fixtures 06 click-90, 04 kick-124, 05 a-minor, 03 voice; rolls 1/4, 1, 4 beats,
 * released off the beat grid), worst click ratio per length:
 *   hard cut -> 40.83, 128 -> 1.15, 256 -> 1.00, 512 -> 1.00
 * Through the deck's Signalsmith worklet the hard seek already measures at most
 * 1.10 and this blend would measure up to 270.1 (its buffer-source old
 * tail is not the phase vocoder's output), so arm() skips processors that
 * crossfade their own seeks. */
export const SLIP_RETURN_BLEND_FRAMES = 128;

const CLICK_GUARD_FRAMES = 16;
const FLOAT32_MIN_NORMAL = 1.1754943508222875e-38;

//----- pure math

function _requireFrames(frames: number): void {
	if (!Number.isInteger(frames) || frames <= 0) {
		throw new RangeError(`frames must be a positive integer, got ${String(frames)}`);
	}
}

/** Linear gains at a frame offset from the switch. The pair always sums to 1, which is
 * what Web Audio's setValueAtTime + linearRampToValueAtTime evaluates per frame. */
export function slipReturnBlendGain(
	frame: number,
	frames: number
): { old: number; incoming: number } {
	_requireFrames(frames);
	if (!Number.isFinite(frame) || frame < 0) {
		throw new RangeError(`frame must be finite and non-negative, got ${String(frame)}`);
	}
	if (frame >= frames) return { old: 0, incoming: 1 };
	const incoming = frame / frames;
	return { old: 1 - incoming, incoming };
}

function _maxAbsDiff(samples: Float32Array, start: number, end: number): number {
	let peak = 0;
	for (let n = start; n < end; n++) peak = Math.max(peak, Math.abs(samples[n] - samples[n - 1]));
	return peak;
}

/** Click ratio, the house definition until the #9774 render harness replaces it.
 * d[n] = |x[n] - x[n-1]|. Over the event window [event - 16, event + 512 + 16),
 * ratio = max d of the render / max d of the sharper of the two sources the seek
 * joins: the looped audio left running past the release, and the no-roll track.
 * So a transient either source already has (a kick at the hidden head, a loop
 * seam, which is #9843) is not counted; only sharpness the seek adds is. A ratio
 * at or under 1.5 passes. */
export function clickRatio(
	samples: Float32Array,
	sources: readonly Float32Array[],
	eventFrame: number
): number {
	const eventStart = eventFrame - CLICK_GUARD_FRAMES;
	const eventEnd = eventFrame + SLIP_RETURN_BLEND_CANDIDATES[2] + CLICK_GUARD_FRAMES;
	if (sources.length === 0) throw new RangeError('click ratio needs at least one source');
	for (const buffer of [samples, ...sources]) {
		if (eventStart < 1 || eventEnd > buffer.length) {
			throw new RangeError(
				`click window [${eventStart}, ${eventEnd}) outside buffer of ${buffer.length}`
			);
		}
	}
	const sourcePeak = Math.max(
		...sources.map((source) => _maxAbsDiff(source, eventStart, eventEnd))
	);
	if (sourcePeak === 0) throw new RangeError('click source window is silent');
	return _maxAbsDiff(samples, eventStart, eventEnd) / sourcePeak;
}

/** Throw on any sample over 0 dBFS, non-finite, or subnormal in [start, end). */
export function assertBlendWindowClean(samples: Float32Array, start: number, end: number): void {
	for (let n = Math.max(0, start); n < Math.min(samples.length, end); n++) {
		const x = samples[n];
		if (!Number.isFinite(x)) throw new RangeError(`non-finite sample ${x} at ${n}`);
		if (Math.abs(x) > 1) throw new RangeError(`sample ${x} over 0 dBFS at ${n}`);
		if (x !== 0 && Math.abs(x) < FLOAT32_MIN_NORMAL)
			throw new RangeError(`subnormal sample at ${n}`);
	}
}

//----- graph glue

interface BlendProcessor {
	disconnect(): void;
	connect(node: AudioNode): void;
}

export interface SlipReturnBlendRuntime {
	audioBuffer: AudioBuffer | null;
	nodes: { analyser: AudioNode } | null;
	processor: BlendProcessor | null;
	loadToken: unknown;
}

export type SlipReturnBlendOutcome =
	'blended' | 'hard:stem-processor' | 'hard:processor-crossfades' | 'hard:past';

export interface SlipReturnBlendHandle {
	arm(effectiveWhen: number): SlipReturnBlendOutcome;
}

/** Build the blend; `arm(effectiveWhen)` is called from inside the schedule's inputSec
 * callback so the ramps land on the exact switch time. effectiveWhen is the audible
 * onset: the engine acknowledges the presented timeline with startContextTime =
 * effectiveWhen (audio-engine.svelte.ts _scheduleDeckSerial, acknowledgePresentedTransportSchedule),
 * the processor lead having been applied before it via processorOnsetLeadSec. */
export function prepareSlipReturnBlend(
	deps: {
		context: AudioContext;
		runtime: SlipReturnBlendRuntime;
		isStemProcessor: boolean;
		/** True for a processor that already overlap-adds across a seek (the Signalsmith
		 * stretch worklet re-reads its input window every block). A buffer-source old tail
		 * does not match such a processor's output, so blending over it adds a step. */
		processorCrossfadesSeeks: boolean;
		/** Loop-folded presented position of the looping (old) audio. */
		oldPositionSecAt: (contextTime: number) => number;
		loop: { startSec: number; endSec: number } | null;
		tempoRatio: number;
	},
	frames: number = SLIP_RETURN_BLEND_FRAMES
): SlipReturnBlendHandle {
	_requireFrames(frames);
	return {
		arm(effectiveWhen: number): SlipReturnBlendOutcome {
			const { context: ctx, runtime: rt } = deps;
			if (deps.isStemProcessor) return 'hard:stem-processor';
			if (deps.processorCrossfadesSeeks) return 'hard:processor-crossfades';
			if (effectiveWhen <= ctx.currentTime) return 'hard:past';
			const buffer = rt.audioBuffer;
			const nodes = rt.nodes;
			const processor = rt.processor;
			if (buffer === null || nodes === null || processor === null) {
				throw new Error('slip return blend requires a loaded deck graph');
			}
			const t0 = effectiveWhen;
			const tEnd = t0 + frames / ctx.sampleRate;

			const mainGain = ctx.createGain();
			const outGain = ctx.createGain();
			const outSrc = ctx.createBufferSource();
			processor.disconnect();
			processor.connect(mainGain);
			mainGain.connect(nodes.analyser);
			outGain.connect(nodes.analyser);
			outSrc.buffer = buffer;
			// Blends under master tempo too, unlike follower-backward-blend: the old tail is at
			// most 512 frames, too short for its resampled pitch to be audible.
			outSrc.playbackRate.value = deps.tempoRatio;
			if (deps.loop !== null) {
				outSrc.loop = true;
				outSrc.loopStart = deps.loop.startSec;
				outSrc.loopEnd = deps.loop.endSec;
			}
			outSrc.connect(outGain);

			mainGain.gain.setValueAtTime(1, ctx.currentTime);
			mainGain.gain.setValueAtTime(0, t0);
			mainGain.gain.linearRampToValueAtTime(1, tEnd);
			outGain.gain.setValueAtTime(0, ctx.currentTime);
			outGain.gain.setValueAtTime(1, t0);
			outGain.gain.linearRampToValueAtTime(0, tEnd);

			const restore = (): void => {
				outSrc.disconnect();
				outGain.disconnect();
				mainGain.disconnect();
				if (rt.processor === processor && rt.nodes !== null) {
					processor.disconnect();
					processor.connect(rt.nodes.analyser);
				}
			};
			try {
				outSrc.start(t0, deps.oldPositionSecAt(t0));
			} catch (error) {
				restore();
				throw error;
			}

			const token = rt.loadToken;
			const delayMs = Math.max(0, (tEnd - ctx.currentTime) * 1000) + 50;
			globalThis.setTimeout(() => {
				outSrc.stop();
				if (rt.loadToken !== token) {
					outSrc.disconnect();
					outGain.disconnect();
					mainGain.disconnect();
					return;
				}
				restore();
			}, delayMs);
			return 'blended';
		}
	};
}
