/**
 * Beat FX quantized engage and Release FX echo (BEATFX-10766, BEATFX-10766b): schedules the
 * beat FX send's ON, OFF and X-PAD moves on the target's beat grid, and the hold-to-apply
 * Release FX echo that drops the beat FX and leaves only a beat echo of the target.
 *
 * Spec: `(private path)` section 4.3 (Quantize, Release FX rows).
 * Mimicked: DJM-A9 manual p. 46 and p. 49 item 1 (quantize on red, gray with no GRID,
 * hidden off); rekordbox 7.2.8 manual p. 179 (Release FX) and p. 183 (pad hold semantics).
 * The boundary rule (next boundary of min(division, 1 beat)) is an Open DJ hygiene choice,
 * not calibrated. Ramp length copied from our own `beat-fx.ts` on `af--b2b-link`.
 *
 * The unit graph (#9782) owns the gains and passes them in; this module allocates nothing
 * per press and takes an injected context so an offline render can prove it.
 */
import {
	BEAT_FX_BEATS,
	BEAT_FX_MAX_BPM,
	BEAT_FX_MIN_BPM,
	nextQuantizeBoundarySec,
	type BeatFxBeats
} from './beat-fx';
import { buildEchoKernel, type EchoKernel, type EchoKernelContext } from './fx/echo-kernel';

//----- constants

/** Every beat FX gain move ramps this long (spec 4.3 Ramps row). */
export const BEAT_FX_RAMP_SEC = 0.012;
/** Release echo loop: the color ECHO numbers (color-fx-graph.ts ECHO_*), mimicked. */
export const BEAT_FX_RELEASE_FEEDBACK = 0.55;
export const BEAT_FX_RELEASE_LOOP_HP_HZ = 180;
export const BEAT_FX_RELEASE_LOOP_LP_HZ = 3000;
/** Longest release echo: 16 beats at the slowest tempo. */
export const BEAT_FX_RELEASE_MAX_DELAY_SEC = Math.max(...BEAT_FX_BEATS) * (60 / BEAT_FX_MIN_BPM);
export const BEAT_FX_QUANTIZE_STATES = ['on', 'no_grid', 'off'] as const;
export type BeatFxQuantizeState = (typeof BEAT_FX_QUANTIZE_STATES)[number];

//----- scheduling (pure)

export interface BeatFxQuantizeInput {
	quantize: boolean;
	/** The target's beat times (s, ascending) already mapped to the AudioContext clock by the caller; [] with no grid. */
	gridBeatsSec: readonly number[];
	/** AudioContext currentTime at the command. */
	nowSec: number;
	beats: BeatFxBeats;
}

export interface BeatFxQuantizePlan {
	state: BeatFxQuantizeState;
	atSec: number;
}

/** When a transition starts: the next boundary of min(beats, 1 beat) with quantize on and a grid, else at once. */
export function planQuantizedTransition(input: BeatFxQuantizeInput): BeatFxQuantizePlan {
	const { quantize, gridBeatsSec, nowSec, beats } = input;
	if (!Number.isFinite(nowSec))
		throw new RangeError(`beat fx nowSec must be a finite number; got ${String(nowSec)}`);
	if (!quantize) return { state: 'off', atSec: nowSec };
	const boundary = nextQuantizeBoundarySec(gridBeatsSec, nowSec, beats);
	if (boundary === null) return { state: 'no_grid', atSec: nowSec };
	return { state: 'on', atSec: boundary };
}

//----- gain moves

const _lastTarget = new WeakMap<AudioParam, number>();

/** Hold `param` at its last scheduled value until `atSec`, then ramp linearly to `value` over BEAT_FX_RAMP_SEC. */
export function scheduleGainStep(param: AudioParam, value: number, atSec: number): void {
	const from = _lastTarget.get(param) ?? param.value;
	param.cancelScheduledValues(atSec);
	param.setValueAtTime(from, atSec);
	param.linearRampToValueAtTime(value, atSec + BEAT_FX_RAMP_SEC);
	_lastTarget.set(param, value);
}

/** Open or close the beat FX send at plan.atSec; ON, OFF, X-PAD touch and X-PAD release all go through here. */
export function scheduleBeatFxEnable(
	send: GainNode,
	enabled: boolean,
	plan: BeatFxQuantizePlan
): void {
	scheduleGainStep(send.gain, enabled ? 1 : 0, plan.atSec);
}

//----- Release FX

export type BeatFxReleaseContext = EchoKernelContext & Pick<BaseAudioContext, 'currentTime'>;

/** The gains on the target the release drives; the unit (#9782) owns and passes them. */
export interface BeatFxReleaseTarget {
	/** The target's dry path gain (1 = dry passes). */
	dry: GainNode;
	/** The beat FX unit's send into its kernel for this target (its tail rings when closed). */
	beatFxSend: GainNode;
	/** Where the target's signal is tapped, pre dry gain, to feed the release echo. */
	tap: AudioNode;
	/** Where the release echo output joins (the target's post-dry sum). */
	out: AudioNode;
}

export interface BeatFxReleasePress {
	beats: BeatFxBeats;
	bpm: number;
	/** Press applies at plan.atSec; callers pass now (only the dry return is quantized). */
	plan: BeatFxQuantizePlan;
}

export interface BeatFxRelease {
	echo: EchoKernel;
	releaseSend: GainNode;
	press(cmd: BeatFxReleasePress): void;
	release(plan: BeatFxQuantizePlan): void;
	held(): boolean;
	nodes: AudioNode[];
	dispose(): void;
}

/** The release echo graph: tap -> releaseSend -> echo kernel -> releaseReturn -> out, built once. */
export function buildBeatFxRelease(
	ctx: BeatFxReleaseContext,
	target: BeatFxReleaseTarget
): BeatFxRelease {
	const echo = buildEchoKernel(ctx, {
		mode: 'echo',
		maxDelaySec: BEAT_FX_RELEASE_MAX_DELAY_SEC,
		feedback: BEAT_FX_RELEASE_FEEDBACK,
		loopHpHz: BEAT_FX_RELEASE_LOOP_HP_HZ,
		loopLpHz: BEAT_FX_RELEASE_LOOP_LP_HZ
	});
	const releaseSend = ctx.createGain();
	releaseSend.gain.value = 0;
	const releaseReturn = ctx.createGain();
	releaseReturn.gain.value = 1;
	target.tap.connect(releaseSend);
	releaseSend.connect(echo.input);
	echo.output.connect(releaseReturn);
	releaseReturn.connect(target.out);
	const nodes: AudioNode[] = [releaseSend, releaseReturn, ...echo.nodes];
	let isHeld = false;

	const press = ({ beats, bpm, plan }: BeatFxReleasePress): void => {
		if (isHeld) throw new Error('beat_fx_release is already held');
		if (!(BEAT_FX_BEATS as readonly number[]).includes(beats))
			throw new RangeError(
				`beat_fx_release beats must be one of ${BEAT_FX_BEATS.join(', ')}; got ${beats}`
			);
		if (!(bpm >= BEAT_FX_MIN_BPM && bpm <= BEAT_FX_MAX_BPM))
			throw new RangeError(
				`beat_fx_release bpm must be in ${BEAT_FX_MIN_BPM}..${BEAT_FX_MAX_BPM}; got ${bpm}`
			);
		echo.setTime((beats * 60) / bpm, ctx.currentTime);
		scheduleGainStep(target.beatFxSend.gain, 0, plan.atSec);
		scheduleGainStep(releaseSend.gain, 1, plan.atSec);
		scheduleGainStep(target.dry.gain, 0, plan.atSec);
		isHeld = true;
	};

	const release = (plan: BeatFxQuantizePlan): void => {
		if (!isHeld) throw new Error('beat_fx_release is not held');
		// releaseReturn and feedback stay put so the release echo rings out; beat FX stays off.
		scheduleGainStep(target.dry.gain, 1, plan.atSec);
		scheduleGainStep(releaseSend.gain, 0, plan.atSec);
		isHeld = false;
	};

	return {
		echo,
		releaseSend,
		press,
		release,
		held: () => isHeld,
		nodes,
		dispose: () => nodes.forEach((node) => node.disconnect())
	};
}
