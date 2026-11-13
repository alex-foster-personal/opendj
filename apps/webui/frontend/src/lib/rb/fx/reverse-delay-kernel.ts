/**
 * REVERSE DELAY graph host (issue #10006): input gate -> `mdt-reverse-delay` worklet -> output.
 *
 * Test-only until the beat FX unit registers it (#10015): it exposes the echo kernel's
 * slot surface (`input`, `output`, `setTime`, `setFeedback`, `gateInput`, `dispose`) so the
 * unit can host it without a special case. Wet only; the caller keeps the dry path.
 * The worklet cannot import, so this module hands it the #9813 soft clip constants and
 * mirrors its parameter table (the unit test pins the two equal).
 */
import reverseDelayProcessorUrl from '$lib/rb/reverse-delay-processor.js?url';

import { ECHO_LOOP_CEILING, ECHO_LOOP_KNEE } from './echo-kernel';

export const REVERSE_DELAY_PROCESSOR_NAME = 'mdt-reverse-delay';
/** Mirror of the processor's table: timeSec k-rate 0.0027..4.0 s, feedback 0..0.95, wet 0..1. */
export const REVERSE_DELAY_PARAMS = {
	timeSec: { min: 0.0027, max: 4.0, default: 0.5 },
	feedback: { min: 0, max: 0.95, default: 0 },
	wet: { min: 0, max: 1, default: 1 }
} as const;
/** Loop state below this is flushed to 0 (no subnormals); same value as the FDN reverb. */
export const REVERSE_DELAY_DENORMAL_FLUSH = 1e-25;
/** Input gate ramp, mimicked from the spec 04 10 ms Release FX cut. */
export const REVERSE_DELAY_GATE_RAMP_SEC = 0.01;

type ReverseDelayParam = keyof typeof REVERSE_DELAY_PARAMS;

export interface ReverseDelayRefusal {
	type: 'refusal';
	reason: string;
	param: ReverseDelayParam;
	value: number;
	min: number;
	max: number;
}

export interface ReverseDelayKernelOptions {
	createWorkletNode(
		ctx: BaseAudioContext,
		name: string,
		options: AudioWorkletNodeOptions
	): AudioWorkletNode;
	/** Every refusal the processor posts (an automation value outside its range). */
	onRefusal(refusal: ReverseDelayRefusal): void;
}

export interface ReverseDelayKernel {
	input: GainNode;
	output: GainNode;
	node: AudioWorkletNode;
	setTime(seconds: number, now: number): void;
	setFeedback(amount: number, now: number): void;
	setWet(amount: number, now: number): void;
	/** Open (true) or close the send into the window capture over the gate ramp. */
	gateInput(open: boolean, now: number): void;
	dispose(): void;
}

const addModuleMemos = new WeakMap<BaseAudioContext, Promise<void>>();

/** Add the processor module to `ctx` once; a failed add is forgotten so the next call retries. */
export function loadReverseDelayWorklet(ctx: BaseAudioContext): Promise<void> {
	let pending = addModuleMemos.get(ctx);
	if (!pending) {
		pending = ctx.audioWorklet.addModule(reverseDelayProcessorUrl).catch((error: unknown) => {
			addModuleMemos.delete(ctx);
			throw error;
		});
		addModuleMemos.set(ctx, pending);
	}
	return pending;
}

/** The processorOptions the worklet requires: the echo soft clip and the denormal flush. */
export function reverseDelayProcessorOptions() {
	return {
		clipKnee: ECHO_LOOP_KNEE,
		clipCeiling: ECHO_LOOP_CEILING,
		denormalFlush: REVERSE_DELAY_DENORMAL_FLUSH
	};
}

function _requireInRange(name: ReverseDelayParam, value: number): void {
	const { min, max } = REVERSE_DELAY_PARAMS[name];
	if (!Number.isFinite(value) || value < min || value > max) {
		throw new RangeError(`reverse delay ${name} must be ${min}..${max}; got ${value}`);
	}
}

/** Build the host around one worklet node; the module must already be loaded on `ctx`. */
export function buildReverseDelayKernel(
	ctx: BaseAudioContext,
	options: ReverseDelayKernelOptions
): ReverseDelayKernel {
	const input = ctx.createGain();
	const output = ctx.createGain();
	const node = options.createWorkletNode(ctx, REVERSE_DELAY_PROCESSOR_NAME, {
		numberOfInputs: 1,
		numberOfOutputs: 1,
		outputChannelCount: [2],
		processorOptions: reverseDelayProcessorOptions()
	});
	node.port.onmessage = (event: MessageEvent<ReverseDelayRefusal>) => options.onRefusal(event.data);
	input.connect(node);
	node.connect(output);
	const set = (name: ReverseDelayParam) => (value: number, now: number) => {
		_requireInRange(name, value);
		node.parameters.get(name)!.setValueAtTime(value, now);
	};
	return {
		input,
		output,
		node,
		setTime: set('timeSec'),
		setFeedback: set('feedback'),
		setWet: set('wet'),
		gateInput(open, now) {
			input.gain.cancelScheduledValues(now);
			input.gain.setValueAtTime(input.gain.value, now);
			input.gain.linearRampToValueAtTime(open ? 1 : 0, now + REVERSE_DELAY_GATE_RAMP_SEC);
		},
		dispose() {
			node.port.onmessage = null;
			input.disconnect();
			node.disconnect();
			output.disconnect();
		}
	};
}
