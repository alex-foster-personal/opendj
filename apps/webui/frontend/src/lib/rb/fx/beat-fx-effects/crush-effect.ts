/**
 * Beat FX BIT CRUSH (issue #10018): the color knob's CRUSH worklet (`color-fx-crush-processor.js`,
 * SNDCOL-10655) as a unit effect, with no second copy of the maths. LEVEL/DEPTH is one knob over
 * bit depth and sample rate, as Traktor Digital LoFi's Group Mode joins BIT and SRTE (Traktor Pro
 * manual, Effect Reference, mimicked): depth d drives the processor as the color knob turned
 * counterclockwise to travel -d at the nominal PARAMETER, so bits 16 -> 4 and decimation 1 -> 64
 * follow the color knob's exponential taper, and the wet share is d, reaching 1.0 at full.
 *
 * The unit's slot is a send and return (the channel dry stays 1) and a crusher is an insert, so
 * the slot returns only what the crusher changes:
 *
 * ```
 * send -> input -+-> crush worklet -> wet (d) --+-> return
 *                +-> cancel (-d) ---------------+
 * dry (x) + return (d * (crush(x) - x)) = (1 - d) * x + d * crush(x)
 * ```
 *
 * At depth 0 both gains are exactly 0, so the return is exactly 0. The processor module loads once
 * per AudioContext through the color knob's `loadCrushWorklet`; until it is ready the input feeds
 * nothing, so the channel plays dry (never dipped by the cancel alone). The beat division has no
 * meaning here (`usesDivision: false`).
 */
import crushProcessorUrl from '../../color-fx-crush-processor.js?url';
import {
	CRUSH_PARAMETER_NOMINAL,
	CRUSH_WET_CROSSFADE_SEC,
	DEFAULT_CRUSH_VOICING,
	createCrushWorkletNode,
	crushProcessorErrorText,
	loadCrushWorklet,
	type CrushLoad
} from '../../color-fx-crush';
import { RecordingKernelHost, type BeatFxKernel } from './kernel-host';
import { registerBeatFxEffect } from './registry';

/** The color knob travel that crushes as hard as LEVEL/DEPTH `depth`: its counterclockwise side. */
export function crushTravelForDepth(depth: number): number {
	if (!(depth >= 0 && depth <= 1)) {
		throw new RangeError(`beat_fx crush depth must be within 0..1, got ${String(depth)}`);
	}
	return -depth;
}

/** Build the crusher for the unit's slot on `load`'s context: the return is d * (crush(x) - x). */
export function createCrushSlotKernel(ctx: BaseAudioContext, load: CrushLoad): BeatFxKernel {
	if (load.state === 'failed') {
		throw new Error(
			`CRUSH_UNAVAILABLE: beat_fx effect crush worklet failed to load: ${load.error}`
		);
	}
	const input = ctx.createGain();
	const wet = ctx.createGain();
	const cancel = ctx.createGain();
	const output = ctx.createGain();
	wet.gain.value = 0;
	cancel.gain.value = 0;
	wet.connect(output);
	cancel.connect(output);
	let node: AudioWorkletNode | null = null;
	let depth = 0;
	let disposed = false;

	const driveTravel = (at: number): void => {
		if (node === null) return;
		const travel = node.parameters.get('travel');
		const parameter = node.parameters.get('parameter');
		if (travel === undefined || parameter === undefined)
			throw new Error('crush worklet node is missing its parameters');
		travel.setValueAtTime(crushTravelForDepth(depth), at);
		parameter.setValueAtTime(CRUSH_PARAMETER_NOMINAL, at);
	};
	const rampGains = (from: number, to: number, at: number): void => {
		for (const [param, sign] of [
			[wet.gain, 1],
			[cancel.gain, -1]
		] as const) {
			param.cancelScheduledValues(at);
			param.setValueAtTime(sign * from, at);
			param.linearRampToValueAtTime(sign * to, at + CRUSH_WET_CROSSFADE_SEC);
		}
	};
	const splice = (): void => {
		if (disposed || node !== null) return;
		node = createCrushWorkletNode(ctx, DEFAULT_CRUSH_VOICING);
		// A terminal processor error silences the node for good: cut the cancel too, so the slot returns 0.
		node.onprocessorerror = (event: Event): void => {
			console.error(
				`[beat-fx] CRUSH_UNAVAILABLE: crush worklet stopped: ${crushProcessorErrorText(event)}`
			);
			input.disconnect();
		};
		input.connect(node).connect(wet);
		input.connect(cancel);
		driveTravel(ctx.currentTime);
		// A splice after the load fades the return in from 0, so a late engage never steps.
		rampGains(0, depth, ctx.currentTime);
	};
	if (load.state === 'ready') splice();
	else if (load.state === 'loading')
		void load.promise.then(() => {
			if (load.state === 'ready') splice();
		});

	return {
		input,
		output,
		setTiming: () => undefined,
		setDepth: (next, at) => {
			crushTravelForDepth(next);
			const from = depth;
			depth = next;
			driveTravel(at);
			if (node !== null) rampGains(from, next, at);
		},
		dispose: () => {
			disposed = true;
			input.disconnect();
			node?.disconnect();
			wet.disconnect();
			cancel.disconnect();
			output.disconnect();
		}
	};
}

registerBeatFxEffect({
	name: 'crush',
	levelLaw: 'depth',
	builtOnMain: true,
	usesDivision: false,
	assertDivisionAllowed: () => undefined,
	createHost: () => new RecordingKernelHost('crush'),
	createKernel: (ctx) => createCrushSlotKernel(ctx, loadCrushWorklet(ctx, crushProcessorUrl))
});
