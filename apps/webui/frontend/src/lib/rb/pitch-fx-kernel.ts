/**
 * Beat FX PITCH kernel: wet copy shifted by ratio 1 + p/100 (-50..100 percent),
 * blended with dry by LEVEL/DEPTH (issue #10222).
 *
 * Latency matches pitch-shift-processor.js: at 0% the wet path is 1 + W/2
 * samples; never more than W plus one render quantum. Dry is bit-exact when
 * depth is 0 or the kernel is off. CPU per-effect row waits on #9777.
 */
import pitchShiftProcessorUrl from '$lib/rb/pitch-shift-processor.js?url';
import { defineVariantSetting } from '$lib/settings/variant-setting';

import type { BeatFxKernel } from './beat-fx';
import { onVariantChange } from './variant-prefs';

export const PITCH_FX_MIN_PERCENT = -50;
export const PITCH_FX_MAX_PERCENT = 100;
export const PITCH_FX_DEFAULT_PERCENT = -50;

export const PITCH_FX_STEPS = Object.freeze({
	'-50%': -50,
	'-37%': -37,
	'-25%': -25,
	'0%': 0,
	'26%': 26,
	'50%': 50,
	'100%': 100
});
export type PitchFxStep = keyof typeof PITCH_FX_STEPS;

const STEP_LABELS = Object.keys(PITCH_FX_STEPS) as PitchFxStep[];

export function parsePitchPercent(value: number | string): number {
	if (typeof value === 'number') {
		if (!Number.isFinite(value) || value < PITCH_FX_MIN_PERCENT || value > PITCH_FX_MAX_PERCENT) {
			throw new RangeError(
				`pitch percent ${value} outside ${PITCH_FX_MIN_PERCENT}..${PITCH_FX_MAX_PERCENT}; steps: ${STEP_LABELS.join(', ')}`
			);
		}
		return value;
	}
	if (typeof value === 'string') {
		if (Object.prototype.hasOwnProperty.call(PITCH_FX_STEPS, value)) {
			return PITCH_FX_STEPS[value as PitchFxStep];
		}
		throw new RangeError(
			`pitch percent ${JSON.stringify(value)} outside ${PITCH_FX_MIN_PERCENT}..${PITCH_FX_MAX_PERCENT}; steps: ${STEP_LABELS.join(', ')}`
		);
	}
	throw new RangeError(
		`pitch percent ${String(value)} outside ${PITCH_FX_MIN_PERCENT}..${PITCH_FX_MAX_PERCENT}; steps: ${STEP_LABELS.join(', ')}`
	);
}

export function pitchRatio(percent: number | string): number {
	return 1 + parsePitchPercent(percent) / 100;
}

export const PITCH_FX_RAMP_SEC = 0.012;
export const PITCH_FX_WINDOW_XFADE_SEC = 0.02;

export const PITCH_FX_WINDOW_VARIANT = defineVariantSetting({
	id: 'variant_fx_pitch_window',
	label: 'PITCH FX window',
	group: 'advanced',
	keywords: ['variant', 'fx', 'pitch', 'window', 'beat fx'],
	detail: 'Crossfade window of the beat FX PITCH shifter (issue #10222).',
	variants: [
		{ value: '20', label: '20 ms', summary: 'tight, more warble' },
		{ value: '40', label: '40 ms', summary: 'balanced' },
		{ value: '80', label: '80 ms', summary: 'smooth, smears transients' }
	],
	defaultValue: '40'
});

export const PITCH_FX_WINDOWS_MS = PITCH_FX_WINDOW_VARIANT.variants.map((v) =>
	Number(v.value)
) as readonly number[];
export const PITCH_FX_MAX_WINDOW_MS = Math.max(...PITCH_FX_WINDOWS_MS);

export function pitchWindowMs(value: string): number {
	const n = Number(value);
	if (!PITCH_FX_WINDOW_VARIANT.variants.some((v) => v.value === value)) {
		throw new RangeError(`unknown PITCH FX window variant ${JSON.stringify(value)}`);
	}
	return n;
}

export const PITCH_SHIFT_PROCESSOR_NAME = 'mdt-pitch-shift';

const addModuleMemos = new WeakMap<BaseAudioContext, Promise<void>>();

export function loadPitchShiftWorklet(ctx: BaseAudioContext): Promise<void> {
	let pending = addModuleMemos.get(ctx);
	if (!pending) {
		pending = ctx.audioWorklet!.addModule(pitchShiftProcessorUrl).catch((error: unknown) => {
			addModuleMemos.delete(ctx);
			throw error;
		});
		addModuleMemos.set(ctx, pending);
	}
	return pending;
}

function windowSamplesAtRate(ms: number, sampleRate: number): number {
	return 2 * Math.round((ms * sampleRate) / 2000);
}

export interface PitchFxKernel extends BeatFxKernel {
	setPercent(p: number | PitchFxStep, at: number): void;
	setActive(on: boolean, at: number): void;
	readonly windowMs: number;
	readonly latencySamples: number;
}

export interface PitchFxKernelOptions {
	readVariant(id: string): string;
	percent?: number | PitchFxStep;
	createWorkletNode?(
		ctx: BaseAudioContext,
		name: string,
		options: AudioWorkletNodeOptions
	): AudioWorkletNode;
}

function assertAudioTime(at: number, field: string): void {
	if (!Number.isFinite(at) || at < 0) {
		throw new RangeError(`${field} requires a finite time >= 0, got ${at}`);
	}
}

export function createPitchFxKernel(
	ctx: BaseAudioContext,
	options: PitchFxKernelOptions
): PitchFxKernel {
	const variantId = PITCH_FX_WINDOW_VARIANT.id;
	let windowMs = pitchWindowMs(options.readVariant(variantId));
	const sampleRate = ctx.sampleRate;

	const input = ctx.createGain();
	const output = ctx.createGain();
	const factory =
		options.createWorkletNode ?? ((c, name, opts) => new AudioWorkletNode(c, name, opts));

	const node = factory(ctx, PITCH_SHIFT_PROCESSOR_NAME, {
		numberOfInputs: 1,
		numberOfOutputs: 1,
		outputChannelCount: [2],
		channelCount: 2,
		channelCountMode: 'explicit',
		processorOptions: {
			windowMs,
			maxWindowMs: PITCH_FX_MAX_WINDOW_MS,
			rampSec: PITCH_FX_RAMP_SEC,
			crossfadeSec: PITCH_FX_WINDOW_XFADE_SEC
		}
	});

	input.connect(node);
	node.connect(output);

	const percentParam = node.parameters.get('percent')!;
	const depthParam = node.parameters.get('depth')!;
	const activeParam = node.parameters.get('active')!;

	const initialPercent = parsePitchPercent(options.percent ?? PITCH_FX_DEFAULT_PERCENT);
	percentParam.setValueAtTime(initialPercent, 0);
	depthParam.setValueAtTime(0, 0);
	activeParam.setValueAtTime(0, 0);

	let disposed = false;

	const unsubscribe = onVariantChange(variantId, (_id, _from, to) => {
		if (disposed) return;
		windowMs = pitchWindowMs(to);
		node.port.postMessage({ kind: 'window', windowMs });
	});

	const kernel: PitchFxKernel = {
		get input() {
			return input;
		},
		get output() {
			return output;
		},
		get windowMs() {
			return windowMs;
		},
		get latencySamples() {
			const W = windowSamplesAtRate(windowMs, sampleRate);
			return 1 + W / 2;
		},
		setTiming(seconds: number, at: number) {
			if (disposed) throw new Error('pitch fx kernel disposed');
			assertAudioTime(at, 'setTiming at');
			if (!Number.isFinite(seconds) || seconds < 0) {
				throw new RangeError(`setTiming seconds must be finite and >= 0, got ${seconds}`);
			}
			// PITCH is percent-based, not tempo-timed (#10234 maps BEAT buttons).
		},
		setDepth(x: number, at: number) {
			if (disposed) throw new Error('pitch fx kernel disposed');
			assertAudioTime(at, 'setDepth at');
			if (!Number.isFinite(x) || x < 0 || x > 1) {
				throw new RangeError(`depth must be finite in [0, 1], got ${x}`);
			}
			depthParam.setValueAtTime(x, at);
		},
		setPercent(p: number | PitchFxStep, at: number) {
			if (disposed) throw new Error('pitch fx kernel disposed');
			assertAudioTime(at, 'setPercent at');
			const parsed = parsePitchPercent(p);
			percentParam.setValueAtTime(parsed, at);
		},
		setActive(on: boolean, at: number) {
			if (disposed) throw new Error('pitch fx kernel disposed');
			assertAudioTime(at, 'setActive at');
			activeParam.setValueAtTime(on ? 1 : 0, at);
		},
		dispose() {
			if (disposed) return;
			disposed = true;
			unsubscribe();
			node.port.postMessage({ kind: 'dispose' });
			input.disconnect();
			node.disconnect();
			output.disconnect();
		}
	};

	return kernel;
}
