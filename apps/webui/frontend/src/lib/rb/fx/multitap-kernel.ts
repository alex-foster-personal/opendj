/**
 * MULTI TAP delay kernel (issue #10009, fx-fanout spec 04-echo-delay section 1). Up to seven
 * repeats in 1/8 steps of the division and no feedback, so a one-beat setting fills the beat
 * with taps. Mimicked: DJM-2000 Operating Instructions p. 22 (up to 7 delay sounds in 1/8 units
 * of the beat fraction, TIME 10 to 4000 ms). The tap level law is not published, so it is an
 * option for the pairs export (#10019): 'flat' (every tap at wet) or 'decay' (tap k at wet times
 * MULTITAP_DECAY^(k-1)).
 *
 * ```
 * caller send -> input (gate) -+-> bank 0: delays[0][k] -> tapGains[0][k] -> bankGains[0] -+-> output
 *                              +-> bank 1: delays[1][k] -> tapGains[1][k] -> bankGains[1] -+   (sum gain)
 * ```
 *
 * Tap k (1..7) delays by k * time / 8. A retune never ramps a live delayTime (that would bend
 * the pitch of every tap): it steps the silent bank's taps and crossfades the two banks over the
 * echo kernel's retune fade (#9810), equal-gain linear as the banks are correlated. The output
 * carries 1 / sqrt(n) for n taps so an in-phase kick through seven flat taps stays under 0 dBFS.
 * Native nodes only, all built once; nothing is allocated after construction. The output is wet
 * only; the caller mixes dry.
 */
import { ECHO_RETUNE_XFADE_SEC } from './echo-kernel';

export const MULTITAP_MAX_TAPS = 7;
/** Taps sit on 1/8 steps of the division. */
export const MULTITAP_STEPS = 8;
export const MULTITAP_TIME_MIN_SEC = 0.01;
export const MULTITAP_TIME_MAX_SEC = 4;
/** Ratio between neighbouring taps under the 'decay' law. */
export const MULTITAP_DECAY = 0.85;
/** Gain, gate and tap count ramps. */
export const MULTITAP_RAMP_SEC = 0.01;
/** Bank crossfade on a retune, shared with the echo kernel. */
export const MULTITAP_RETUNE_XFADE_SEC = ECHO_RETUNE_XFADE_SEC;
export const MULTITAP_LAWS = ['flat', 'decay'] as const;
export type MultiTapLaw = (typeof MULTITAP_LAWS)[number];
/** The law the issue names as the default until the #10019 rating picks one. */
export const MULTITAP_LAW_DEFAULT: MultiTapLaw = 'decay';

export type MultiTapKernelContext = Pick<BaseAudioContext, 'createGain' | 'createDelay'>;

export interface MultiTapKernelOptions {
	/** The division length in seconds; tap k lands at k * timeSec / 8. */
	timeSec: number;
	/** Active taps, 1..7. */
	taps: number;
	/** Wet level 0..1. */
	wet: number;
	multitapLaw: MultiTapLaw;
}

type Bank<T> = readonly [T, T];

export interface MultiTapKernel {
	/** The caller's send lands here; gateInput ramps only this gain. */
	input: GainNode;
	/** Wet sum of both banks, scaled by multitapSumGain(taps). */
	output: GainNode;
	/** delays[bank][k - 1] is tap k of that bank. */
	delays: Bank<readonly DelayNode[]>;
	tapGains: Bank<readonly GainNode[]>;
	bankGains: Bank<GainNode>;
	/** Retune to `seconds` at context time `now` through the bank crossfade. */
	setTime(seconds: number, now: number): void;
	/** The time the kernel settles on (the newest setTime). */
	time(): number;
	/** Ramp to `n` active taps (1..7) over MULTITAP_RAMP_SEC; delays never move. */
	setTaps(n: number, now: number): void;
	taps(): number;
	/** Ramp the wet level (0..1) over MULTITAP_RAMP_SEC. */
	setWet(wet: number, now: number): void;
	/** Ramp the input alone open or shut; taps already in the lines ring out. */
	gateInput(open: boolean, now: number): void;
	dispose(): void;
	nodes: AudioNode[];
}

type Fade = { start: number; end: number; to: 0 | 1 };

const _ms = (seconds: number): string => `${Math.round(seconds * 1e4) / 10}`;

/** Throw unless `seconds` is a finite time inside the DJM-2000 TIME range of 10 to 4000 ms. */
export function assertMultiTapTime(seconds: number): void {
	if (
		!Number.isFinite(seconds) ||
		seconds < MULTITAP_TIME_MIN_SEC ||
		seconds > MULTITAP_TIME_MAX_SEC
	) {
		throw new RangeError(
			`multitap time ${_ms(seconds)} ms is outside [${_ms(MULTITAP_TIME_MIN_SEC)}, ${_ms(MULTITAP_TIME_MAX_SEC)}] ms`
		);
	}
}

/** Throw unless `n` is an integer tap count in 1..7. */
export function assertMultiTapCount(n: number): void {
	if (!Number.isInteger(n) || n < 1 || n > MULTITAP_MAX_TAPS) {
		throw new RangeError(`multitap taps ${n} is outside 1..${MULTITAP_MAX_TAPS}`);
	}
}

function _assertWet(wet: number): void {
	if (!Number.isFinite(wet) || wet < 0 || wet > 1) {
		throw new RangeError(`multitap wet ${wet} is outside [0, 1]`);
	}
}

function _assertLaw(law: string): void {
	if (!(MULTITAP_LAWS as readonly string[]).includes(law)) {
		throw new RangeError(`multitap law ${law} is not one of ${MULTITAP_LAWS.join(', ')}`);
	}
}

/** Delay of tap k (1-based) for a division of `seconds`. */
export function multitapTapDelaySec(k: number, seconds: number): number {
	return (k * seconds) / MULTITAP_STEPS;
}

/** The seven tap gains for `law` with `n` taps active at `wet`; inactive taps are 0. */
export function multitapTapGains(law: MultiTapLaw, n: number, wet: number): number[] {
	return Array.from({ length: MULTITAP_MAX_TAPS }, (_, i) =>
		i >= n ? 0 : law === 'flat' ? wet : wet * MULTITAP_DECAY ** i
	);
}

/** Output gain for `n` taps: 1 / sqrt(n) keeps seven in-phase flat taps of a kick under 0 dBFS. */
export function multitapSumGain(n: number): number {
	return 1 / Math.sqrt(n);
}

function _gain(ctx: MultiTapKernelContext, value: number): GainNode {
	const node = ctx.createGain();
	node.gain.value = value;
	return node;
}

/** Ramp linearly from where the param is to `value` over MULTITAP_RAMP_SEC. */
function _ramp(param: AudioParam, value: number, now: number): void {
	param.cancelScheduledValues(now);
	param.setValueAtTime(param.value, now);
	param.linearRampToValueAtTime(value, now + MULTITAP_RAMP_SEC);
}

/** Build the multi-tap kernel once on `ctx`; see the module doc for the graph. */
export function buildMultiTapKernel(
	ctx: MultiTapKernelContext,
	options: MultiTapKernelOptions
): MultiTapKernel {
	assertMultiTapTime(options.timeSec);
	assertMultiTapCount(options.taps);
	_assertWet(options.wet);
	_assertLaw(options.multitapLaw);
	const law = options.multitapLaw;
	let newest = options.timeSec;
	let count = options.taps;
	let wetLevel = options.wet;
	let on: 0 | 1 = 0;
	let last: Fade | null = null;
	let disposed = false;

	const gains = multitapTapGains(law, count, wetLevel);
	const input = _gain(ctx, 1);
	const output = _gain(ctx, multitapSumGain(count));
	const _bank = (live: boolean) => {
		const delays = gains.map((_, i) => {
			const delay = ctx.createDelay(multitapTapDelaySec(i + 1, MULTITAP_TIME_MAX_SEC));
			delay.delayTime.value = multitapTapDelaySec(i + 1, newest);
			return delay;
		});
		const taps = gains.map((g) => _gain(ctx, g));
		const bank = _gain(ctx, live ? 1 : 0);
		delays.forEach((delay, i) => {
			input.connect(delay);
			delay.connect(taps[i]);
			taps[i].connect(bank);
		});
		bank.connect(output);
		return { delays, taps, bank };
	};
	const b0 = _bank(true);
	const b1 = _bank(false);
	const delays = [b0.delays, b1.delays] as const;
	const tapGains = [b0.taps, b1.taps] as const;
	const bankGains = [b0.bank, b1.bank] as const;

	const _live = (): void => {
		if (disposed) throw new Error('multitap kernel is disposed');
	};

	const _stepBank = (bank: 0 | 1, seconds: number, at: number): void => {
		delays[bank].forEach((delay, i) => {
			delay.delayTime.cancelScheduledValues(at);
			delay.delayTime.setValueAtTime(multitapTapDelaySec(i + 1, seconds), at);
		});
	};

	const _rampTaps = (now: number): void => {
		const targets = multitapTapGains(law, count, wetLevel);
		for (const bank of tapGains) bank.forEach((tap, i) => _ramp(tap.gain, targets[i], now));
	};

	const setTime = (seconds: number, now: number): void => {
		_live();
		assertMultiTapTime(seconds);
		if (last !== null && last.start > now) {
			// The queued fade has not begun, so its bank is still silent: retarget it.
			_stepBank(last.to, seconds, last.start);
			newest = seconds;
			return;
		}
		if (seconds === newest) return;
		const start = Math.max(now, last?.end ?? -Infinity);
		const end = start + MULTITAP_RETUNE_XFADE_SEC;
		const from = on;
		const to = (1 - on) as 0 | 1;
		_stepBank(to, seconds, start);
		bankGains[from].gain.setValueAtTime(1, start);
		bankGains[from].gain.linearRampToValueAtTime(0, end);
		bankGains[to].gain.setValueAtTime(0, start);
		bankGains[to].gain.linearRampToValueAtTime(1, end);
		last = { start, end, to };
		on = to;
		newest = seconds;
	};

	const setTaps = (n: number, now: number): void => {
		_live();
		assertMultiTapCount(n);
		count = n;
		_rampTaps(now);
		_ramp(output.gain, multitapSumGain(n), now);
	};

	const setWet = (wet: number, now: number): void => {
		_live();
		_assertWet(wet);
		wetLevel = wet;
		_rampTaps(now);
	};

	const gateInput = (open: boolean, now: number): void => {
		_live();
		_ramp(input.gain, open ? 1 : 0, now);
	};

	const nodes: AudioNode[] = [input, ...delays.flat(), ...tapGains.flat(), ...bankGains, output];

	const dispose = (): void => {
		_live();
		disposed = true;
		for (const node of nodes) node.disconnect();
	};

	return {
		input,
		output,
		delays,
		tapGains,
		bankGains,
		setTime,
		time: () => newest,
		setTaps,
		taps: () => count,
		setWet,
		gateInput,
		dispose,
		nodes
	};
}
