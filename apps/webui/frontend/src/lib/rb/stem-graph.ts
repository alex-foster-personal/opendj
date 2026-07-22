/**
 * Real precomputed-Demucs graph for Rekordbox VOCAL / INST / DRUMS controls.
 *
 * Requirements:
 *   ✔︎ Four decoded parts must be sample-aligned before graph publication.
 *     [if] any part differs in sample rate, frame count, channels, or duration
 *       [then] construction fails before a processor is connected
 *   ✔︎ INST owns the real bass + other branches.
 *     [if] INST is muted [then] both branches gain 0 without subtraction
 *   ✔︎ One schedule is acknowledged only after every branch acknowledges.
 *     [if] a branch schedule fails [then] the group rejects and disconnects
 *   ✔︎ Mute/solo state is deterministic and serializable.
 *     [if] any group is soloed [then] non-solo groups gain 0; mute wins
 */

import {
	StretchDeckProcessor,
	type StretchAdapterOptions,
	type StretchScheduleChange
} from '$lib/rb/stretch-adapter';
import type {
	StemAlignment,
	StemControl,
	StemControlState,
	StemDeckState
} from '$lib/rb/types';

export const STEM_CONTROLS: readonly StemControl[] = ['vocal', 'instrumental', 'drums'];
export const DEMUCS_PARTS = ['vocals', 'drums', 'bass', 'other'] as const;
export type DemucsPart = (typeof DEMUCS_PARTS)[number];

export type StemControls = Record<StemControl, StemControlState>;
export type StemBuffers = Record<DemucsPart, AudioBuffer>;

export interface StemArtifactIdentity {
	source: 'demucs';
	model: string;
}

function _exactKeys(name: string, value: object, keys: readonly string[]): void {
	const actual = Object.keys(value).sort();
	const expected = [...keys].sort();
	if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
		throw new TypeError(`${name} must contain exactly ${expected.join(', ')}`);
	}
}

export function createDefaultStemControls(): StemControls {
	return {
		vocal: { muted: false, solo: false },
		instrumental: { muted: false, solo: false },
		drums: { muted: false, solo: false }
	};
}

export function unavailableStemDeckState(error: string | null = null): StemDeckState {
	return {
		status: 'unavailable',
		source: null,
		model: null,
		alignment: null,
		controls: createDefaultStemControls(),
		error
	};
}

export function readyStemDeckState(
	identity: StemArtifactIdentity,
	alignment: StemAlignment
): StemDeckState {
	return {
		status: 'ready',
		source: identity.source,
		model: identity.model,
		alignment: { ...alignment },
		controls: createDefaultStemControls(),
		error: null
	};
}

function _validateControls(controls: StemControls): void {
	if (typeof controls !== 'object' || controls === null) {
		throw new TypeError('stem controls must be an object');
	}
	_exactKeys('stem controls', controls, STEM_CONTROLS);
	for (const stem of STEM_CONTROLS) {
		const state = controls[stem];
		if (typeof state !== 'object' || state === null) {
			throw new TypeError(`stem controls.${stem} must be an object`);
		}
		_exactKeys(`stem controls.${stem}`, state, ['muted', 'solo']);
		if (typeof state.muted !== 'boolean' || typeof state.solo !== 'boolean') {
			throw new TypeError(`stem controls.${stem} mute/solo values must be boolean`);
		}
	}
}

export function stemPartGains(controls: StemControls): Record<DemucsPart, 0 | 1> {
	_validateControls(controls);
	const anySolo = STEM_CONTROLS.some((stem) => controls[stem].solo);
	const gain = (stem: StemControl): 0 | 1 => {
		const state = controls[stem];
		return !state.muted && (!anySolo || state.solo) ? 1 : 0;
	};
	return {
		vocals: gain('vocal'),
		drums: gain('drums'),
		bass: gain('instrumental'),
		other: gain('instrumental')
	};
}

export function validateStemBufferAlignment(buffers: StemBuffers): StemAlignment {
	if (typeof buffers !== 'object' || buffers === null) {
		throw new TypeError('stem buffers must be an object');
	}
	_exactKeys('stem buffers', buffers, DEMUCS_PARTS);
	const reference = buffers.vocals;
	if (
		!Number.isFinite(reference.sampleRate) ||
		reference.sampleRate <= 0 ||
		!Number.isInteger(reference.length) ||
		reference.length <= 0 ||
		!Number.isInteger(reference.numberOfChannels) ||
		reference.numberOfChannels < 1 ||
		reference.numberOfChannels > 2 ||
		!Number.isFinite(reference.duration) ||
		reference.duration <= 0
	) {
		throw new RangeError('stem alignment reference metadata is invalid');
	}
	for (const part of DEMUCS_PARTS) {
		const buffer = buffers[part];
		if (
			buffer.sampleRate !== reference.sampleRate ||
			buffer.length !== reference.length ||
			buffer.numberOfChannels !== reference.numberOfChannels ||
			Math.abs(buffer.duration - reference.duration) > 1 / reference.sampleRate
		) {
			throw new Error(
				`stem alignment mismatch for ${part}: expected ${reference.sampleRate}Hz, ` +
					`${reference.length} frames, ${reference.numberOfChannels} channels, ` +
					`${reference.duration}s; got ${buffer.sampleRate}Hz, ${buffer.length} frames, ` +
					`${buffer.numberOfChannels} channels, ${buffer.duration}s`
			);
		}
	}
	return {
		sample_rate_hz: reference.sampleRate,
		frame_count: reference.length,
		channel_count: reference.numberOfChannels,
		duration_ms: reference.duration * 1000
	};
}

function _aggregateFailures(operation: string, outcomes: readonly PromiseSettledResult<void>[]): void {
	const failures = outcomes.flatMap((outcome) =>
		outcome.status === 'rejected' ? [outcome.reason] : []
	);
	if (failures.length > 0) {
		throw new AggregateError(failures, `aligned stem ${operation} failed on ${failures.length} branch(es)`);
	}
}

export interface SchedulableStemProcessor {
	schedule(outputTime: number, change: StretchScheduleChange): Promise<void>;
}

export async function scheduleAlignedStemProcessors(
	processors: Record<DemucsPart, SchedulableStemProcessor>,
	outputTime: number,
	change: StretchScheduleChange
): Promise<void> {
	_exactKeys('stem processors', processors, DEMUCS_PARTS);
	const outcomes = await Promise.allSettled(
		DEMUCS_PARTS.map((part) => processors[part].schedule(outputTime, change))
	);
	_aggregateFailures('schedule', outcomes);
}

/** Four real time-stretch processors sharing exact schedules and summing into
 * the existing deck analyser. No original-mix bypass exists once selected. */
export class AlignedStemDeckProcessor {
	readonly #context: AudioContext;
	readonly #processors: Record<DemucsPart, StretchDeckProcessor>;
	readonly #gains: Record<DemucsPart, GainNode>;
	readonly #latencySec: number;
	#controls: StemControls = createDefaultStemControls();

	private constructor(
		context: AudioContext,
		processors: Record<DemucsPart, StretchDeckProcessor>,
		gains: Record<DemucsPart, GainNode>,
		latencySec: number
	) {
		this.#context = context;
		this.#processors = processors;
		this.#gains = gains;
		this.#latencySec = latencySec;
	}

	static async create(
		context: AudioContext,
		buffers: StemBuffers,
		options: StretchAdapterOptions
	): Promise<{ processor: AlignedStemDeckProcessor; alignment: StemAlignment }> {
		const alignment = validateStemBufferAlignment(buffers);
		const processors = {} as Record<DemucsPart, StretchDeckProcessor>;
		const gains = {} as Record<DemucsPart, GainNode>;
		try {
			for (const part of DEMUCS_PARTS) {
				const processor = await StretchDeckProcessor.create(context, options);
				await processor.load(buffers[part]);
				processors[part] = processor;
				const gain = context.createGain();
				gain.gain.value = 1;
				processor.connect(gain);
				gains[part] = gain;
			}
			const latencies = await Promise.all(
				DEMUCS_PARTS.map((part) => processors[part].latencySec())
			);
			const latencySec = latencies[0];
			if (latencies.some((latency) => Math.abs(latency - latencySec) > 1 / alignment.sample_rate_hz)) {
				throw new Error(`stem processor latency alignment mismatch: ${latencies.join(', ')}`);
			}
			return {
				processor: new AlignedStemDeckProcessor(context, processors, gains, latencySec),
				alignment
			};
		} catch (error) {
			const cleanupFailures: unknown[] = [];
			for (const part of DEMUCS_PARTS) {
				try {
					processors[part]?.disconnect();
				} catch (cleanupError) {
					cleanupFailures.push(cleanupError);
				}
				try {
					gains[part]?.disconnect();
				} catch (cleanupError) {
					cleanupFailures.push(cleanupError);
				}
			}
			if (cleanupFailures.length > 0) {
				throw new AggregateError(
					[error, ...cleanupFailures],
					'stem graph construction and cleanup failed'
				);
			}
			throw error;
		}
	}

	connect(destination: AudioNode): void {
		for (const part of DEMUCS_PARTS) this.#gains[part].connect(destination);
	}

	disconnect(): void {
		const failures: unknown[] = [];
		for (const part of DEMUCS_PARTS) {
			try {
				this.#processors[part].disconnect();
			} catch (error) {
				failures.push(error);
			}
			try {
				this.#gains[part].disconnect();
			} catch (error) {
				failures.push(error);
			}
		}
		if (failures.length === 1) throw failures[0];
		if (failures.length > 1) throw new AggregateError(failures, 'stem graph disconnect failed');
	}

	async latencySec(): Promise<number> {
		return this.#latencySec;
	}

	async schedule(outputTime: number, change: StretchScheduleChange): Promise<void> {
		try {
			await scheduleAlignedStemProcessors(this.#processors, outputTime, change);
		} catch (error) {
			try {
				this.disconnect();
			} catch (disconnectError) {
				throw new AggregateError(
					[error, disconnectError],
					'stem schedule and graph disconnect failed'
				);
			}
			throw error;
		}
	}

	async stop(outputTime: number): Promise<void> {
		await this.schedule(outputTime, { active: false });
	}

	setControls(controls: StemControls): void {
		_validateControls(controls);
		const gains = stemPartGains(controls);
		for (const part of DEMUCS_PARTS) {
			this.#gains[part].gain.setTargetAtTime(gains[part], this.#context.currentTime, 0.01);
		}
		this.#controls = {
			vocal: { ...controls.vocal },
			instrumental: { ...controls.instrumental },
			drums: { ...controls.drums }
		};
	}

	controls(): StemControls {
		return {
			vocal: { ...this.#controls.vocal },
			instrumental: { ...this.#controls.instrumental },
			drums: { ...this.#controls.drums }
		};
	}
}
