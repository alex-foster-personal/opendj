/**
 * Shared color-echo loop (fx-fanout spec 04-echo-delay section 4.1): one loop sum feeds
 * two DelayNodes whose taps crossfade on retune. Ramping delayTime resamples what is
 * already in the line and bends pitch on every repeat (Traktor Delay retunes without
 * glitches by stepping time on the silent line instead). The two taps come from the
 * same loop so they are correlated: equal-gain complementary linear ramps keep level
 * flat; equal-power would bump mid-fade by up to 3 dB.
 *
 * ```
 * caller send -> input (gate) -> loopSum -> softClip -> clipMakeup -+-> lines[0] -> lineGains[0] -+-> lineSum
 *                                                                  +-> lines[1] -> lineGains[1] -+    |
 *                       ^---- feedback <- loopLp <-+- loopHp <---------------------------------------+
 *                       ^---- tapeFeedback <-------+   (TAPE: HP only)
 *                       ^---- holdFeedback <----------------------------------------------------+   (HELIX hold: straight)
 * lineSum -> wet ---------------------------------------------------> output -> caller return
 * lineSum -> mono -+-> straight[0] ------------------> merger L -> pingWet -^
 *  (width makeup)  +-> cross[1] ---------------------> merger R
 *                  +-> pong (width * time / 2) -> pongGain -+-> straight[1] -> merger R
 *                                                           +-> cross[0] ----> merger L
 * ```
 *
 * PING PONG width (#9992, spec 04 section 4.1 Ping-pong offset row; DJM-900NXS2 pp. 16-17 and
 * Traktor Delay SPRD, mimicked): the right line's offset is width * time / 2 and each side takes
 * (1 + width) / 2 of its own line and (1 - width) / 2 of the other, so width 0 is ECHO on both
 * sides and width 1 is hard L/R alternation. The mono fold (L + R) / 2 is mono * (line + offset
 * line) / 2 at every width: two decorrelated half copies lose 3 dB. Each side stays at unity (main's
 * level) by default; options.pingpongMakeup gives mono an equal-power makeup sqrt(1 + width) and drives
 * the loop as much, clipMakeup undoing it (as TAPE) under the guard ceiling. A makeup rise waits one
 * line time, until the line holds only content clipped at the new drive; a fall is at once.
 *
 * Modes (DJM-900NXS2 Operating Instructions p. 17, mimicked): `delay` outputs the delayed sound
 * once (loop feedback forced to 0), `echo` repeats it, attenuated by the feedback and darkened
 * by the loop HP and LP, `pingpong` is the echo loop split into a left line at the full time
 * and a right line fed from it, offset by half the time, so repeats alternate L, R every half
 * time. `tape` (ours, spec 04 section 4.1; behaviour after Traktor Tape Delay) is the echo loop
 * with three changes: a retune glides the live line's delayTime (setTargetAtTime, time constant
 * ECHO_TAPE_GLIDE_SEC) so the repeats bend in pitch; the loop runs HP only (tapeFeedback taps
 * loopHp, loopLp is out); and the soft clip is driven ECHO_TAPE_DRIVE_DB harder (loopSum gain)
 * with the same makeup after it (clipMakeup), so small signals pass at unity and the loop
 * saturates 6 dB earlier, at most at the ceiling over the drive. A mode is one row in
 * ECHO_MODE_ROUTING; switching is a 10 ms gain crossfade (the drive pair ramps exponentially, so
 * drive times makeup stays exactly 1).
 *
 * HELIX hold (spec 04 section 4.1 Freeze row; DJM-900NXS2 Types of BEAT FX pp. 16-17, mimicked):
 * setHold(level) raises the loop gain linearly from the mode's value to HELIX_HOLD_FEEDBACK as the
 * level goes 0 to 1. At 1 the input gate closes and the loop moves, on a 10 ms complementary
 * crossfade, from the filtered path (feedback or tapeFeedback) to holdFeedback, a straight path
 * past loopHp and loopLp, so the held loop does not darken (Traktor FRZ keeps the filter in and
 * cannot hold forever). The soft clip stays in the loop, so unity feedback cannot clip. A retune
 * while held glides like TAPE (options.holdGlide, default true, the HELIX pitch bend) or crossfades.
 *
 * Headroom guard (spec 04 section 4.1, mimicked from Mixxx saturating inside its delay
 * line): a static soft clip on the loop sum, identity below -6 dBFS with a tanh knee to a
 * 0.9 ceiling. FXECHO-03's gain-sum proof drives the send at or below `ECHO_LOOP_KNEE` so
 * the clip is identity and line taps track complementary gains on a flat send.
 *
 * Native nodes only, built once, nothing allocated per render quantum. The only
 * delayTime automation is setValueAtTime, except the TAPE glide (setTargetAtTime). The glide's
 * first pitch factor is 1 - (time change / time constant): a jump longer than the constant runs
 * the repeats briefly backwards, and it settles within 5 cents after 4 constants only for a jump
 * under about 0.16 constants (a tempo-follow step); the glide time is calibrated by #9816.
 */
/** Old-to-new line crossfade on a retune (spec 04 section 4.1). */
export const ECHO_RETUNE_XFADE_SEC = 0.03;

/** Soft clip knee (-6 dBFS): the curve is exact identity at or below it. */
export const ECHO_LOOP_KNEE = 10 ** (-6 / 20);
/** Offline gain-sum proofs (FXECHO-03 e2e) keep the send at or below the knee. */
export const ECHO_GAIN_PROBE_DC = ECHO_LOOP_KNEE * 0.99;
/** The curve approaches but never reaches this level. */
export const ECHO_LOOP_CEILING = 0.9;
/** 2^12 + 1 points: 0 maps to the centre and the 1/2048 grid is exact in float32. */
export const ECHO_LOOP_CURVE_LENGTH = 4097;
/** Longest beat FX echo line (DJM-900NXS2 TIME 1 to 4000 ms, p. 17), mimicked. */
export const ECHO_LINE_CEILING_SEC = 4;
/** Highest loop gain per repeat: an Open DJ safety bound (no published reference). */
export const ECHO_FEEDBACK_MAX = 0.95;
/** Input gate and mode switch ramp (spec 04 section 4.1 Trails row). */
export const ECHO_GATE_RAMP_SEC = 0.01;
/** TAPE retune glide time constant, the spec 04 start value (calibrated by #9816). */
export const ECHO_TAPE_GLIDE_SEC = 0.12;
/** TAPE soft clip drive in dB, made up after the clip (#9814 start value, calibrated by #9816). */
export const ECHO_TAPE_DRIVE_DB = 6;
export const ECHO_TAPE_DRIVE = 10 ** (ECHO_TAPE_DRIVE_DB / 20);
/** Loop gain at full HELIX hold: unity, reached only through setHold (setFeedback stays clamped). */
export const HELIX_HOLD_FEEDBACK = 1.0;
/** PING PONG width at build, 1 is hard L/R alternation (taste: #10019 rates 0.5 against 1.0). */
export const PINGPONG_WIDTH_DEFAULT = 1.0;
/** Web Audio render quantum: a delay in a cycle cannot be shorter. */
const RENDER_QUANTUM_FRAMES = 128;

/** Per mode: does the loop feed back, is the output split L/R, is it the tape loop. HELIX is setHold, not a row. */
export const ECHO_MODE_ROUTING = {
	delay: { loops: false, pingPong: false, tape: false },
	echo: { loops: true, pingPong: false, tape: false },
	pingpong: { loops: true, pingPong: true, tape: false },
	tape: { loops: true, pingPong: false, tape: true }
} as const satisfies Record<string, { loops: boolean; pingPong: boolean; tape: boolean }>;
export type EchoMode = keyof typeof ECHO_MODE_ROUTING;
export const ECHO_MODES = Object.keys(ECHO_MODE_ROUTING) as EchoMode[];

export type EchoKernelContext = Pick<
	BaseAudioContext,
	| 'createGain'
	| 'createDelay'
	| 'createBiquadFilter'
	| 'createWaveShaper'
	| 'createChannelMerger'
	| 'sampleRate'
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
	mode: EchoMode;
	/** Line buffer length (seconds); setTime refuses anything longer. */
	maxDelaySec: number;
	/** Loop gain per repeat, in [0, ECHO_FEEDBACK_MAX]; the same check as setFeedback. */
	feedback: number;
	loopHpHz: number;
	loopLpHz: number;
	/** TAPE glide time constant (seconds) for the A/B page; ECHO_TAPE_GLIDE_SEC when absent. */
	tapeGlideSec?: number;
	/** A retune while held glides (true, the default, HELIX) or crossfades (false). */
	holdGlide?: boolean;
	/** PING PONG width in [0, 1] for the A/B page; PINGPONG_WIDTH_DEFAULT when absent. */
	pingpongWidth?: number;
	/** Equal-power width makeup (true) for the A/B page; absent or false is unity per side, main's level. */
	pingpongMakeup?: boolean;
}

export interface EchoKernel {
	/** The caller's send lands here; gateInput ramps only this gain. */
	input: GainNode;
	/** The loop sum: the gated send and the feedback both land here; its gain is the TAPE drive. */
	loopSum: GainNode;
	/** Makeup after the soft clip: 1, or 1 / ECHO_TAPE_DRIVE in TAPE. */
	clipMakeup: GainNode;
	/** Static soft clip on the loop sum (spec 04 section 4.1): identity below -6 dBFS, tanh knee to a 0.9 ceiling. */
	softClip: WaveShaperNode;
	/** Sum of the two gated line taps; the loop and the output stage read it. */
	lineSum: GainNode;
	/** What the caller's return reads: lineSum in delay/echo, the L/R split in pingpong. */
	output: GainNode;
	wet: GainNode;
	pingWet: GainNode;
	/** Mono downmix of lineSum; its gain is the width makeup in pingpong, 1 otherwise. */
	mono: GainNode;
	/** The right line (width * time / 2) and its retune duck. */
	pong: DelayNode;
	pongGain: GainNode;
	/** Own-side gains, mono to L and offset line to R: (1 + width) / 2. */
	straight: readonly [GainNode, GainNode];
	/** Other-side gains, offset line to L and mono to R: (1 - width) / 2. */
	cross: readonly [GainNode, GainNode];
	/** The two delay lines and their crossfade gains (index 0 is live at build). */
	lines: readonly [DelayNode, DelayNode];
	lineGains: readonly [GainNode, GainNode];
	loopHp: BiquadFilterNode;
	loopLp: BiquadFilterNode;
	/** Loop gain through loopHp and loopLp (delay 0, echo, pingpong). */
	feedback: GainNode;
	/** Loop gain through loopHp alone (TAPE only, 0 otherwise). */
	tapeFeedback: GainNode;
	/** Straight loop gain past both loop filters: HELIX_HOLD_FEEDBACK while held, 0 otherwise. */
	holdFeedback: GainNode;
	/** Move the echo to `seconds` at context time `now` (cold: a step; warm: a crossfade, TAPE: a glide). */
	setTime(seconds: number, now: number): void;
	/** The time the kernel settles on (the newest setTime), or null before the first. */
	time(): number | null;
	/** Loop gain per repeat from `now`; throws outside [0, ECHO_FEEDBACK_MAX]. Delay mode keeps the loop at 0. */
	setFeedback(feedback: number, now: number): void;
	/** Crossfade to `mode`'s routing over ECHO_GATE_RAMP_SEC. */
	setMode(mode: EchoMode, now: number): void;
	mode(): EchoMode;
	/** Ramp the input alone open or shut over ECHO_GATE_RAMP_SEC; the loop rings out (trails). Held, it stays shut. */
	gateInput(open: boolean, now: number): void;
	/** HELIX hold law at `level` in [0, 1] over ECHO_GATE_RAMP_SEC: loop gain to unity, at 1 input shut and filters out. */
	setHold(level: number, now: number): void;
	/** PING PONG width in [0, 1] over ECHO_GATE_RAMP_SEC; throws outside the range or in any other mode. */
	setWidth(width: number, now: number): void;
	width(): number;
	/** Disconnect every node; any later call throws. */
	dispose(): void;
	/** Every node, for the caller's disposal list. */
	nodes: AudioNode[];
}

type Fade = {
	start: number;
	end: number;
	from: 0 | 1;
	to: 0 | 1;
	fromSeconds: number;
	fromOffset: number;
};

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

/** Ramp linearly from where the param is to `value` over ECHO_GATE_RAMP_SEC, holding until `start`. */
function _ramp(param: AudioParam, value: number, now: number, start = now): void {
	param.cancelScheduledValues(now);
	param.setValueAtTime(param.value, now);
	if (start > now) param.setValueAtTime(param.value, start);
	param.linearRampToValueAtTime(value, start + ECHO_GATE_RAMP_SEC);
}

/** Exponential ramp over ECHO_GATE_RAMP_SEC: two such ramps to reciprocal values keep a product of 1. */
function _rampExp(param: AudioParam, value: number, now: number): void {
	param.cancelScheduledValues(now);
	param.setValueAtTime(param.value, now);
	param.exponentialRampToValueAtTime(value, now + ECHO_GATE_RAMP_SEC);
}

/** Throw unless `feedback` is a finite loop gain in [0, ECHO_FEEDBACK_MAX]. */
export function assertEchoFeedback(feedback: number): void {
	if (!Number.isFinite(feedback) || feedback < 0 || feedback > ECHO_FEEDBACK_MAX) {
		throw new RangeError(`echo feedback ${feedback} is outside [0, ${ECHO_FEEDBACK_MAX}]`);
	}
}

/** Throw unless `width` is a finite PING PONG width in [0, 1]. */
export function assertPingpongWidth(width: number): void {
	if (!Number.isFinite(width) || width < 0 || width > 1) {
		throw new RangeError(`echo width ${width} is outside [0, 1]`);
	}
}

/** Equal-power width makeup: the mono fold of the full-width split keeps ECHO's power. */
export function pingpongWidthMakeup(width: number): number {
	return Math.sqrt(1 + width);
}

function _routing(mode: EchoMode): (typeof ECHO_MODE_ROUTING)[EchoMode] {
	if (!Object.hasOwn(ECHO_MODE_ROUTING, mode)) {
		throw new RangeError(`echo mode ${mode} is not one of ${ECHO_MODES.join(', ')}`);
	}
	return ECHO_MODE_ROUTING[mode];
}

const _ms = (seconds: number): string => `${Math.round(seconds * 1e4) / 10}`;

function _waveshaper(ctx: EchoKernelContext, curve: Float32Array<ArrayBuffer>): WaveShaperNode {
	const node = ctx.createWaveShaper();
	node.curve = curve;
	node.oversample = 'none';
	return node;
}

/** Build the echo kernel once on `ctx` in `options.mode`; see the module doc for the graph. */
export function buildEchoKernel(ctx: EchoKernelContext, options: EchoKernelOptions): EchoKernel {
	const { maxDelaySec } = options;
	if (!Number.isFinite(maxDelaySec) || maxDelaySec <= 0) {
		throw new RangeError(`echo line ${maxDelaySec} s must be a positive length`);
	}
	if (!Number.isFinite(ctx.sampleRate) || ctx.sampleRate <= 0) {
		throw new RangeError(`echo kernel needs a positive sampleRate, got ${ctx.sampleRate}`);
	}
	assertEchoFeedback(options.feedback);
	const tapeGlideSec = options.tapeGlideSec ?? ECHO_TAPE_GLIDE_SEC;
	const holdGlide = options.holdGlide ?? true;
	const pingMakeup = options.pingpongMakeup ?? false;
	let width = options.pingpongWidth ?? PINGPONG_WIDTH_DEFAULT;
	assertPingpongWidth(width);
	if (!Number.isFinite(tapeGlideSec) || tapeGlideSec <= 0) {
		throw new RangeError(`echo tape glide ${tapeGlideSec} s must be a positive time constant`);
	}
	const minTimeSec = RENDER_QUANTUM_FRAMES / ctx.sampleRate;
	let current: EchoMode = options.mode;
	let routing = _routing(current);
	let loopGain = options.feedback;
	let hold = 0;
	let inputOpen = true;
	let disposed = false;
	/** Soft clip drive for `r`: TAPE's, the width makeup in pingpong, else 1 (clipMakeup undoes it). */
	const _drive = (r: (typeof ECHO_MODE_ROUTING)[EchoMode]): number => {
		if (r.tape) return ECHO_TAPE_DRIVE;
		return r.pingPong && pingMakeup ? pingpongWidthMakeup(width) : 1;
	};
	let drive = _drive(routing);
	let makeup = routing.pingPong ? drive : 1;

	const input = _gain(ctx, 1);
	const loopSum = _gain(ctx, drive);
	const softClip = _waveshaper(ctx, echoLoopSoftClipCurve());
	const clipMakeup = _gain(ctx, 1 / drive);
	const lineSum = _gain(ctx, 1);
	const line0 = ctx.createDelay(maxDelaySec);
	const line1 = ctx.createDelay(maxDelaySec);
	const lines = [line0, line1] as const;
	const lineGains = [_gain(ctx, 1), _gain(ctx, 0)] as const;
	const loopHp = _biquad(ctx, 'highpass', options.loopHpHz);
	const loopLp = _biquad(ctx, 'lowpass', options.loopLpHz);
	const feedback = _gain(ctx, routing.loops && !routing.tape ? loopGain : 0);
	const tapeFeedback = _gain(ctx, routing.tape ? loopGain : 0);
	const holdFeedback = _gain(ctx, 0);
	const wet = _gain(ctx, routing.pingPong ? 0 : 1);
	const mono = _gain(ctx, makeup);
	mono.channelCount = 1;
	mono.channelCountMode = 'explicit';
	mono.channelInterpretation = 'speakers';
	const pong = ctx.createDelay(maxDelaySec / 2);
	const pongGain = _gain(ctx, 1);
	const straight = [_gain(ctx, (1 + width) / 2), _gain(ctx, (1 + width) / 2)] as const;
	const cross = [_gain(ctx, (1 - width) / 2), _gain(ctx, (1 - width) / 2)] as const;
	const merger = ctx.createChannelMerger(2);
	const pingWet = _gain(ctx, routing.pingPong ? 1 : 0);
	const output = _gain(ctx, 1);

	input.connect(loopSum);
	loopSum.connect(softClip);
	softClip.connect(clipMakeup);
	clipMakeup.connect(line0);
	clipMakeup.connect(line1);
	line0.connect(lineGains[0]);
	line1.connect(lineGains[1]);
	lineGains[0].connect(lineSum);
	lineGains[1].connect(lineSum);
	lineSum.connect(loopHp);
	loopHp.connect(loopLp);
	loopLp.connect(feedback);
	feedback.connect(loopSum);
	loopHp.connect(tapeFeedback);
	tapeFeedback.connect(loopSum);
	lineSum.connect(holdFeedback);
	holdFeedback.connect(loopSum);
	lineSum.connect(wet);
	wet.connect(output);
	lineSum.connect(mono);
	mono.connect(straight[0]);
	straight[0].connect(merger, 0, 0);
	mono.connect(cross[1]);
	cross[1].connect(merger, 0, 1);
	mono.connect(pong);
	pong.connect(pongGain);
	pongGain.connect(straight[1]);
	straight[1].connect(merger, 0, 1);
	pongGain.connect(cross[0]);
	cross[0].connect(merger, 0, 0);
	merger.connect(pingWet);
	pingWet.connect(output);

	const _live = (): void => {
		if (disposed) throw new Error('echo kernel is disposed');
	};

	/** Filtered loop gain for `r` at the hold level: the mode's value rising to unity, 0 once held. */
	const _filteredLoop = (r: (typeof ECHO_MODE_ROUTING)[EchoMode]): number => {
		if (hold === 1) return 0;
		const base = r.loops ? loopGain : 0;
		return base + (HELIX_HOLD_FEEDBACK - base) * hold;
	};

	/** Move the soft clip drive and its reciprocal makeup together (exponential, product stays 1). */
	const _setDrive = (next: number, now: number): void => {
		if (next === drive) return;
		drive = next;
		_rampExp(loopSum.gain, next, now);
		_rampExp(clipMakeup.gain, 1 / next, now);
	};

	/** Ramp the width makeup on mono: a rise waits one line time (the loop's old, hotter content). */
	const _setMakeup = (next: number, now: number): void => {
		if (next === makeup) return;
		const start = next > makeup ? now + (newest ?? 0) : now;
		makeup = next;
		_ramp(mono.gain, next, now, start);
	};

	let pongOffset = 0;
	/** Duck the right line over `span` around a step of its offset (it has one tap, so no crossfade). */
	const _stepPong = (offset: number, start: number, span: number): void => {
		if (offset === pongOffset) return;
		pongOffset = offset;
		const mid = start + span / 2;
		pongGain.gain.setValueAtTime(1, start);
		pongGain.gain.linearRampToValueAtTime(0, mid);
		pong.delayTime.setValueAtTime(offset, mid);
		pongGain.gain.linearRampToValueAtTime(1, start + span);
	};

	let on: 0 | 1 = 0;
	let newest: number | null = null;
	let last: Fade | null = null;
	let prior: Fade | null = null;

	const setTime = (seconds: number, now: number): void => {
		_live();
		if (!Number.isFinite(seconds) || seconds < minTimeSec || seconds > maxDelaySec) {
			throw new RangeError(
				`echo time ${_ms(seconds)} ms is outside the ${_ms(minTimeSec)} ms render quantum to ${_ms(maxDelaySec)} ms line`
			);
		}

		if (newest === null) {
			// Cold start: step on the live line; echoIn stays closed until the line is timed.
			lines[on].delayTime.setValueAtTime(seconds, now);
			pongOffset = (seconds * width) / 2;
			pong.delayTime.setValueAtTime(pongOffset, now);
			newest = seconds;
			return;
		}

		if (seconds === newest) return;

		if (last !== null && last.start > now) {
			lineGains[0].gain.cancelScheduledValues(last.start);
			lineGains[1].gain.cancelScheduledValues(last.start);
			lines[last.to].delayTime.cancelScheduledValues(last.start);
			pongGain.gain.cancelScheduledValues(last.start);
			pong.delayTime.cancelScheduledValues(last.start);
			pongGain.gain.linearRampToValueAtTime(1, last.start);
			if (prior !== null) {
				lineGains[prior.from].gain.linearRampToValueAtTime(0, prior.end);
				lineGains[prior.to].gain.linearRampToValueAtTime(1, prior.end);
			}
			const undone = last;
			on = undone.from;
			newest = undone.fromSeconds;
			pongOffset = undone.fromOffset;
			last = prior;
			prior = null;
			if (seconds === newest) {
				// A width step cancelled with the fade is rescheduled where that fade began.
				_stepPong((newest * width) / 2, undone.start, ECHO_GATE_RAMP_SEC);
				return;
			}
		}

		if (hold === 1 ? holdGlide : routing.tape) {
			// TAPE (or a HELIX hold): glide the live line (and the offset line) so the repeats bend; no crossfade.
			lines[on].delayTime.setTargetAtTime(seconds, now, tapeGlideSec);
			pongOffset = (seconds * width) / 2;
			pong.delayTime.setTargetAtTime(pongOffset, now, tapeGlideSec);
			newest = seconds;
			return;
		}

		const start = Math.max(now, last?.end ?? -Infinity);
		const from = on;
		const to = (1 - on) as 0 | 1;

		lines[to].delayTime.setValueAtTime(seconds, start);
		lineGains[from].gain.setValueAtTime(1, start);
		lineGains[from].gain.linearRampToValueAtTime(0, start + ECHO_RETUNE_XFADE_SEC);
		lineGains[to].gain.setValueAtTime(0, start);
		lineGains[to].gain.linearRampToValueAtTime(1, start + ECHO_RETUNE_XFADE_SEC);
		const fromOffset = pongOffset;
		_stepPong((seconds * width) / 2, start, ECHO_RETUNE_XFADE_SEC);

		prior = last;
		last = {
			start,
			end: start + ECHO_RETUNE_XFADE_SEC,
			from,
			to,
			fromSeconds: newest,
			fromOffset
		};
		on = to;
		newest = seconds;
	};

	const setFeedback = (value: number, now: number): void => {
		_live();
		assertEchoFeedback(value);
		loopGain = value;
		if (routing.loops) {
			_ramp(routing.tape ? tapeFeedback.gain : feedback.gain, _filteredLoop(routing), now);
		}
	};

	const setMode = (mode: EchoMode, now: number): void => {
		_live();
		const next = _routing(mode);
		if (mode === current) return;
		_setDrive(_drive(next), now);
		_setMakeup(next.pingPong ? _drive(next) : 1, now);
		current = mode;
		routing = next;
		_ramp(feedback.gain, next.tape ? 0 : _filteredLoop(next), now);
		_ramp(tapeFeedback.gain, next.tape ? _filteredLoop(next) : 0, now);
		_ramp(wet.gain, next.pingPong ? 0 : 1, now);
		_ramp(pingWet.gain, next.pingPong ? 1 : 0, now);
	};

	const gateInput = (open: boolean, now: number): void => {
		_live();
		inputOpen = open;
		_ramp(input.gain, open && hold < 1 ? 1 : 0, now);
	};

	const setHold = (level: number, now: number): void => {
		_live();
		if (!Number.isFinite(level) || level < 0 || level > 1) {
			throw new RangeError(`echo hold ${level} is outside [0, 1]`);
		}
		hold = level;
		_ramp(input.gain, inputOpen && level < 1 ? 1 : 0, now);
		_ramp(routing.tape ? tapeFeedback.gain : feedback.gain, _filteredLoop(routing), now);
		_ramp(holdFeedback.gain, level === 1 ? HELIX_HOLD_FEEDBACK : 0, now);
	};

	const setWidth = (value: number, now: number): void => {
		_live();
		assertPingpongWidth(value);
		if (!routing.pingPong) throw new Error(`echo width needs pingpong mode, not ${current}`);
		width = value;
		_setDrive(_drive(routing), now);
		_setMakeup(drive, now);
		for (const g of straight) _ramp(g.gain, (1 + value) / 2, now);
		for (const g of cross) _ramp(g.gain, (1 - value) / 2, now);
		if (newest !== null) {
			_stepPong((newest * value) / 2, Math.max(now, last?.end ?? now), ECHO_GATE_RAMP_SEC);
		}
	};

	const nodes: AudioNode[] = [
		input,
		loopSum,
		softClip,
		clipMakeup,
		line0,
		line1,
		lineGains[0],
		lineGains[1],
		lineSum,
		loopHp,
		loopLp,
		feedback,
		tapeFeedback,
		holdFeedback,
		wet,
		mono,
		pong,
		pongGain,
		...straight,
		...cross,
		merger,
		pingWet,
		output
	];

	const dispose = (): void => {
		_live();
		disposed = true;
		for (const node of nodes) node.disconnect();
	};

	return {
		input,
		loopSum,
		clipMakeup,
		softClip,
		lineSum,
		output,
		wet,
		pingWet,
		mono,
		pong,
		pongGain,
		straight,
		cross,
		lines,
		lineGains,
		loopHp,
		loopLp,
		feedback,
		tapeFeedback,
		holdFeedback,
		setTime,
		time: () => newest,
		setFeedback,
		setMode,
		mode: () => current,
		gateInput,
		setHold,
		setWidth,
		width: () => width,
		dispose,
		nodes
	};
}
