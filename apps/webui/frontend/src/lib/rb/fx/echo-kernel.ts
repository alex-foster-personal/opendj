/**
 * Shared color-echo loop (fx-fanout spec 04-echo-delay section 4.1): one loop sum feeds
 * two DelayNodes whose taps crossfade on retune. Ramping delayTime resamples what is
 * already in the line and bends pitch on every repeat (Traktor Delay retunes without
 * glitches by stepping time on the silent line instead). The two taps come from the
 * same loop so they are correlated: equal-gain complementary linear ramps keep level
 * flat; equal-power would bump mid-fade by up to 3 dB.
 *
 * ```
 * caller send -> input -> softClip -+-> lines[0] -> lineGains[0] -+-> output -> caller return
 *                                   +-> lines[1] -> lineGains[1] -+        |
 *                  ^---- feedback <- loopLp <- loopHp <-------------------+
 * ```
 *
 * Headroom guard (spec 04 section 4.1, mimicked from Mixxx saturating inside its delay
 * line): a static soft clip on the loop sum, identity below -6 dBFS with a tanh knee to a
 * 0.9 ceiling. No line can hold more than the curve's maximum and the two taps' gains sum
 * to 1, so the output stays under 0 dBFS at any feedback.
 *
 * Native nodes only, built once, nothing allocated per render quantum. The only
 * delayTime automation is setValueAtTime; TAPE-mode glide (#9814) will be the sole
 * exception when it lands.
 */
/** Old-to-new line crossfade on a retune (spec 04 section 4.1). */
export const ECHO_RETUNE_XFADE_SEC = 0.03;

/** Soft clip knee (-6 dBFS): the curve is exact identity at or below it. */
export const ECHO_LOOP_KNEE = 10 ** (-6 / 20);
/** The curve approaches but never reaches this level. */
export const ECHO_LOOP_CEILING = 0.9;
/** 2^12 + 1 points: 0 maps to the centre and the 1/2048 grid is exact in float32. */
export const ECHO_LOOP_CURVE_LENGTH = 4097;

export type EchoKernelContext = Pick<
	BaseAudioContext,
	'createGain' | 'createDelay' | 'createBiquadFilter' | 'createWaveShaper'
>;

/** Static soft clip curve: identity to the knee, tanh knee above it toward the ceiling. */
export function echoLoopSoftClipCurve(): Float32Array<ArrayBuffer> {
	const n = ECHO_LOOP_CURVE_LENGTH;
	const span = ECHO_LOOP_CEILING - ECHO_LOOP_KNEE;
	const curve = new Float32Array(n);
	for (let k = 0; k < n; k++) {
		const x = (2 * k) / (n - 1) - 1;
		const mag = Math.abs(x);
		curve[k] =
			mag <= ECHO_LOOP_KNEE
				? x
				: Math.sign(x) * (ECHO_LOOP_KNEE + span * Math.tanh((mag - ECHO_LOOP_KNEE) / span));
	}
	return curve;
}

export interface EchoKernelOptions {
	/** Line buffer length (seconds); setTime refuses anything longer. */
	maxDelaySec: number;
	/** Loop gain per repeat. */
	feedback: number;
	loopHpHz: number;
	loopLpHz: number;
}

export interface EchoKernel {
	/** The loop sum: the caller's send and the feedback both land here. */
	input: GainNode;
	/** Static soft clip on the loop sum (spec 04 section 4.1): identity below -6 dBFS, tanh knee to a 0.9 ceiling. */
	softClip: WaveShaperNode;
	/** Sum of the two gated line taps; the caller's return reads it. */
	output: GainNode;
	/** The two delay lines and their crossfade gains (index 0 is live at build). */
	lines: readonly [DelayNode, DelayNode];
	lineGains: readonly [GainNode, GainNode];
	loopHp: BiquadFilterNode;
	loopLp: BiquadFilterNode;
	feedback: GainNode;
	/** Move the echo to `seconds` at context time `now` (cold: a step; warm: a crossfade). */
	setTime(seconds: number, now: number): void;
	/** The time the kernel settles on (the newest setTime), or null before the first. */
	time(): number | null;
	/** Every node, for the caller's disposal list. */
	nodes: AudioNode[];
}

type Fade = { start: number; end: number; from: 0 | 1; to: 0 | 1; fromSeconds: number };

function _gain(ctx: EchoKernelContext, value: number): GainNode {
	const node = ctx.createGain();
	node.gain.value = value;
	return node;
}

function _biquad(ctx: EchoKernelContext, type: BiquadFilterType, hz: number): BiquadFilterNode {
	const node = ctx.createBiquadFilter();
	node.type = type;
	node.frequency.value = hz;
	return node;
}

function _waveshaper(ctx: EchoKernelContext, curve: Float32Array<ArrayBuffer>): WaveShaperNode {
	const node = ctx.createWaveShaper();
	node.curve = curve;
	node.oversample = 'none';
	return node;
}

export function buildEchoKernel(ctx: EchoKernelContext, options: EchoKernelOptions): EchoKernel {
	const input = _gain(ctx, 1);
	const softClip = _waveshaper(ctx, echoLoopSoftClipCurve());
	const output = _gain(ctx, 1);
	const line0 = ctx.createDelay(options.maxDelaySec);
	const line1 = ctx.createDelay(options.maxDelaySec);
	const lines = [line0, line1] as const;
	const lineGains = [_gain(ctx, 1), _gain(ctx, 0)] as const;
	const loopHp = _biquad(ctx, 'highpass', options.loopHpHz);
	const loopLp = _biquad(ctx, 'lowpass', options.loopLpHz);
	const feedback = _gain(ctx, options.feedback);

	input.connect(softClip);
	softClip.connect(line0);
	softClip.connect(line1);
	line0.connect(lineGains[0]);
	line1.connect(lineGains[1]);
	lineGains[0].connect(output);
	lineGains[1].connect(output);
	output.connect(loopHp);
	loopHp.connect(loopLp);
	loopLp.connect(feedback);
	feedback.connect(input);

	let on: 0 | 1 = 0;
	let newest: number | null = null;
	let last: Fade | null = null;
	let prior: Fade | null = null;

	const setTime = (seconds: number, now: number): void => {
		const maxDelaySec = options.maxDelaySec;
		if (!Number.isFinite(seconds) || seconds <= 0 || seconds > maxDelaySec) {
			throw new RangeError(`echo time ${seconds} s is outside (0, ${maxDelaySec}] s`);
		}

		if (newest === null) {
			// Cold start: step on the live line; echoIn stays closed until the line is timed.
			lines[on].delayTime.setValueAtTime(seconds, now);
			newest = seconds;
			return;
		}

		if (seconds === newest) return;

		if (last !== null && last.start > now) {
			lineGains[0].gain.cancelScheduledValues(last.start);
			lineGains[1].gain.cancelScheduledValues(last.start);
			lines[last.to].delayTime.cancelScheduledValues(last.start);
			if (prior !== null) {
				lineGains[prior.from].gain.linearRampToValueAtTime(0, prior.end);
				lineGains[prior.to].gain.linearRampToValueAtTime(1, prior.end);
			}
			on = last.from;
			newest = last.fromSeconds;
			last = prior;
			prior = null;
			if (seconds === newest) return;
		}

		const start = Math.max(now, last?.end ?? -Infinity);
		const from = on;
		const to = (1 - on) as 0 | 1;

		lines[to].delayTime.setValueAtTime(seconds, start);
		lineGains[from].gain.setValueAtTime(1, start);
		lineGains[from].gain.linearRampToValueAtTime(0, start + ECHO_RETUNE_XFADE_SEC);
		lineGains[to].gain.setValueAtTime(0, start);
		lineGains[to].gain.linearRampToValueAtTime(1, start + ECHO_RETUNE_XFADE_SEC);

		prior = last;
		last = {
			start,
			end: start + ECHO_RETUNE_XFADE_SEC,
			from,
			to,
			fromSeconds: newest
		};
		on = to;
		newest = seconds;
	};

	const nodes: AudioNode[] = [
		input,
		softClip,
		line0,
		line1,
		lineGains[0],
		lineGains[1],
		output,
		loopHp,
		loopLp,
		feedback
	];

	return {
		input,
		softClip,
		output,
		lines,
		lineGains,
		loopHp,
		loopLp,
		feedback,
		setTime,
		time: () => newest,
		nodes
	};
}
