/**
 * SPIRAL (fx-fanout spec 04-echo-delay section 3, SPIRAL row; issue #10003): the tape-style
 * glided echo with a Schroeder allpass diffuser inside the loop, so every repeat smears
 * further into a reverberant tail, and a retune glides delayTime so the repeats bend in pitch.
 *
 * ```
 * caller send -> input -+-> dry ---------------------------------------------------+-> output
 *                       +-> gate -> loopSum -> line -> stage 1..n -> softClip -+-> wet -+
 *                                     ^-- feedback <- loopLp <- loopHp <------+
 * ```
 *
 * Mimicked (DJM-900NXS Operating Instructions p. 17, DJM-900NXS2 pp. 16-17): a reverb-like
 * echo whose pitch changes with the delay time; TIME 10 to 4000 ms; LEVEL/DEPTH sets the
 * balance of original and effect sound and the feedback together. Balance law mimicked from
 * rekordbox 7.2.8 pp. 177-178: depth 0 is original only, depth 1 is effect only.
 *
 * Ours: the diffuser (each stage one DelayNode with feedback and feedforward GainNodes, prime
 * times 5 to 23 ms, g = 0.5), Butterworth loop filters (no resonant peak, so loop gain stays
 * under the feedback at every frequency), and the soft clip from the echo kernel placed after
 * the diffuser so the wet output can never exceed the 0.9 ceiling at any depth.
 *
 * Native nodes only, built once at construction, nothing created afterwards.
 */
import { echoLoopSoftClipCurve } from './echo-kernel';

/** Shortest and longest SPIRAL time (DJM TIME row, mimicked). */
export const SPIRAL_MIN_TIME_SEC = 0.01;
export const SPIRAL_MAX_TIME_SEC = 4;
/** Retune glide time constant (spec 04 TAPE start value, shared with #9814). */
export const SPIRAL_GLIDE_SEC = 0.12;
/** Depth and input gate ramp (spec 04 section 4.1 Trails row). */
export const SPIRAL_RAMP_SEC = 0.01;
/** Feedback at depth 0 and depth 1 (issue #10003 law). */
export const SPIRAL_FEEDBACK_MIN = 0.3;
export const SPIRAL_FEEDBACK_MAX = 0.9;
/** Allpass coefficient and prime stage times (ms), longest first; a variant takes the first n. */
export const SPIRAL_ALLPASS_GAIN = 0.5;
export const SPIRAL_ALLPASS_MS = [23, 19, 17, 13, 11, 7] as const;
/** Butterworth Q in the Web Audio dB form (20 log10 of 1/sqrt 2). */
export const SPIRAL_LOOP_Q_DB = 20 * Math.log10(Math.SQRT1_2);

/** Diffusion density variants (taste, for the #10019 pairs export and the #10020 session). */
export const SPIRAL_VARIANT_STAGES = { light: 2, medium: 4, dense: 6 } as const;
export type SpiralVariant = keyof typeof SPIRAL_VARIANT_STAGES;
export const SPIRAL_DEFAULT_VARIANT: SpiralVariant = 'medium';

export type SpiralKernelContext = Pick<
	BaseAudioContext,
	'createGain' | 'createDelay' | 'createBiquadFilter' | 'createWaveShaper'
>;

export interface SpiralKernelOptions {
	/** Starting LEVEL/DEPTH in [0, 1]. */
	depth: number;
	loopHpHz: number;
	loopLpHz: number;
	/** Diffuser density; SPIRAL_DEFAULT_VARIANT when absent. */
	spiralVariant?: SpiralVariant;
	/** Retune glide time constant (seconds); SPIRAL_GLIDE_SEC when absent. */
	glideSec?: number;
}

export interface SpiralAllpassStage {
	/** The stage's summing node: the previous stage lands here with the stage's own feedback. */
	input: GainNode;
	delay: DelayNode;
	feedback: GainNode;
	feedforward: GainNode;
	/** Every node the stage made. */
	nodes: AudioNode[];
	/** Wire the stage's output (delay plus feedforward) into `next`. */
	connectTo(next: AudioNode): void;
}

export interface SpiralKernel {
	/** The caller's send: it feeds the dry path and the gated loop. */
	input: GainNode;
	/** Dry plus wet, at the depth balance. */
	output: GainNode;
	line: DelayNode;
	stages: readonly SpiralAllpassStage[];
	softClip: WaveShaperNode;
	feedback: GainNode;
	dry: GainNode;
	wet: GainNode;
	gate: GainNode;
	/** Move the echo to `seconds` at `now` (cold: a step; warm: a glide that bends pitch). */
	setTime(seconds: number, now: number): void;
	/** Set LEVEL/DEPTH: feedback 0.30..0.90 and wet 0..1 (dry 1..0) linearly, one 10 ms ramp. */
	setDepth(depth: number, now: number): void;
	/** Ramp the input alone open or shut over SPIRAL_RAMP_SEC; the loop rings out (trails). */
	gateInput(open: boolean, now: number): void;
	/** Disconnect every node; any later call throws. */
	dispose(): void;
	/** Every node, for the caller's disposal list. */
	nodes: AudioNode[];
}

//----- helpers

function _gain(ctx: SpiralKernelContext, value: number): GainNode {
	const node = ctx.createGain();
	node.gain.value = value;
	return node;
}

function _butterworth(
	ctx: SpiralKernelContext,
	type: BiquadFilterType,
	hz: number
): BiquadFilterNode {
	const node = ctx.createBiquadFilter();
	node.type = type;
	node.frequency.value = hz;
	node.Q.value = SPIRAL_LOOP_Q_DB;
	return node;
}

function _ramp(param: AudioParam, value: number, now: number): void {
	param.cancelScheduledValues(now);
	param.setValueAtTime(param.value, now);
	param.linearRampToValueAtTime(value, now + SPIRAL_RAMP_SEC);
}

/** Throw unless `depth` is a finite LEVEL/DEPTH in [0, 1]. */
export function assertSpiralDepth(depth: number): void {
	if (!Number.isFinite(depth) || depth < 0 || depth > 1) {
		throw new RangeError(`spiral depth ${depth} is outside [0, 1]`);
	}
}

/** Feedback for a depth on the issue #10003 law: 0.30 at depth 0, 0.90 at depth 1, linear. */
export function spiralFeedbackForDepth(depth: number): number {
	assertSpiralDepth(depth);
	return SPIRAL_FEEDBACK_MIN + (SPIRAL_FEEDBACK_MAX - SPIRAL_FEEDBACK_MIN) * depth;
}

//----- build

/** One Schroeder allpass, w = x + g w[n-D], y = w[n-D] - g w: unity magnitude at every frequency. */
export function buildSpiralAllpassStage(
	ctx: SpiralKernelContext,
	delaySec: number,
	g: number
): SpiralAllpassStage {
	const input = _gain(ctx, 1);
	const delay = ctx.createDelay(delaySec);
	delay.delayTime.value = delaySec;
	const feedback = _gain(ctx, g);
	const feedforward = _gain(ctx, -g);
	input.connect(delay);
	delay.connect(feedback);
	feedback.connect(input);
	input.connect(feedforward);
	const connectTo = (next: AudioNode): void => {
		delay.connect(next);
		feedforward.connect(next);
	};
	return {
		input,
		delay,
		feedback,
		feedforward,
		nodes: [input, delay, feedback, feedforward],
		connectTo
	};
}

/** Build the SPIRAL kernel: glided line, allpass diffuser in the loop, one depth control. */
export function buildSpiralKernel(
	ctx: SpiralKernelContext,
	options: SpiralKernelOptions
): SpiralKernel {
	assertSpiralDepth(options.depth);
	const variant = options.spiralVariant ?? SPIRAL_DEFAULT_VARIANT;
	const stageCount = SPIRAL_VARIANT_STAGES[variant];
	if (stageCount === undefined)
		throw new RangeError(`spiral variant ${String(variant)} is unknown`);
	const glideSec = options.glideSec ?? SPIRAL_GLIDE_SEC;
	if (!Number.isFinite(glideSec) || glideSec <= 0) {
		throw new RangeError(`spiral glide ${glideSec} s must be a positive time constant`);
	}

	const input = _gain(ctx, 1);
	const output = _gain(ctx, 1);
	const dry = _gain(ctx, 1 - options.depth);
	const wet = _gain(ctx, options.depth);
	const gate = _gain(ctx, 1);
	const loopSum = _gain(ctx, 1);
	const line = ctx.createDelay(SPIRAL_MAX_TIME_SEC);
	const stages = SPIRAL_ALLPASS_MS.slice(0, stageCount).map((ms) =>
		buildSpiralAllpassStage(ctx, ms / 1000, SPIRAL_ALLPASS_GAIN)
	);
	const softClip = ctx.createWaveShaper();
	softClip.curve = echoLoopSoftClipCurve();
	softClip.oversample = 'none';
	const loopHp = _butterworth(ctx, 'highpass', options.loopHpHz);
	const loopLp = _butterworth(ctx, 'lowpass', options.loopLpHz);
	const feedback = _gain(ctx, spiralFeedbackForDepth(options.depth));

	input.connect(dry);
	dry.connect(output);
	input.connect(gate);
	gate.connect(loopSum);
	loopSum.connect(line);
	line.connect(stages[0].input);
	stages.forEach((stage, k) => stage.connectTo(stages[k + 1]?.input ?? softClip));
	softClip.connect(wet);
	wet.connect(output);
	softClip.connect(loopHp);
	loopHp.connect(loopLp);
	loopLp.connect(feedback);
	feedback.connect(loopSum);

	const nodes: AudioNode[] = [
		input,
		output,
		dry,
		wet,
		gate,
		loopSum,
		line,
		...stages.flatMap((stage) => stage.nodes),
		softClip,
		loopHp,
		loopLp,
		feedback
	];
	let timed = false;
	let disposed = false;
	const live = (): void => {
		if (disposed) throw new Error('spiral kernel is disposed');
	};

	const setTime = (seconds: number, now: number): void => {
		live();
		if (
			!Number.isFinite(seconds) ||
			seconds < SPIRAL_MIN_TIME_SEC ||
			seconds > SPIRAL_MAX_TIME_SEC
		) {
			throw new RangeError(
				`spiral time ${+(seconds * 1000).toFixed(3)} ms is outside [${SPIRAL_MIN_TIME_SEC * 1000}, ${SPIRAL_MAX_TIME_SEC * 1000}] ms`
			);
		}
		if (!timed) {
			line.delayTime.setValueAtTime(seconds, now);
			timed = true;
			return;
		}
		line.delayTime.cancelScheduledValues(now);
		line.delayTime.setTargetAtTime(seconds, now, glideSec);
	};

	const setDepth = (depth: number, now: number): void => {
		live();
		const loopGain = spiralFeedbackForDepth(depth);
		_ramp(feedback.gain, loopGain, now);
		_ramp(wet.gain, depth, now);
		_ramp(dry.gain, 1 - depth, now);
	};

	const gateInput = (open: boolean, now: number): void => {
		live();
		_ramp(gate.gain, open ? 1 : 0, now);
	};

	const dispose = (): void => {
		live();
		disposed = true;
		for (const node of nodes) node.disconnect();
	};

	return {
		input,
		output,
		line,
		stages,
		softClip,
		feedback,
		dry,
		wet,
		gate,
		setTime,
		setDepth,
		gateInput,
		dispose,
		nodes
	};
}
