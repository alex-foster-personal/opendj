/**
 * Strict typed boundary around the pinned signalsmith-stretch Web Audio
 * package. The package ships without TypeScript declarations and its worklet
 * protocol has two integration hazards which stay local to this adapter:
 * `numberOfInputs` must remain one even with no connected live input, and
 * schedule pruning reads `outputTime` while the public README documents
 * `output`.
 *
 * Requirements (mini-PRD):
 *   ✔︎ Keep one disconnected input and one fixed stereo output.
 *     [if] a processor is created [then] exact safe node options are passed
 *   ✔︎ Duplicate every schedule time into output and outputTime.
 *     [if] a scheduled change is built [then] both fields are equal
 *   ✔︎ Fail explicitly on creation/command timeout or processor failure.
 *     [if] the worklet does not acknowledge [then] the caller receives a
 *       typed timeout error and no alternate audio path is selected
 *     [if] a timeout or processor error becomes terminal [then] later
 *       commands reject before invoking the worklet
 *   ✔︎ Transfer copied mono/stereo PCM into the processor.
 *     [if] decoded audio has more than two channels [then] loading rejects
 */

import createSignalsmithStretch from 'signalsmith-stretch';
import type {
	SignalsmithStretchNode,
	SignalsmithStretchSchedule
} from 'signalsmith-stretch';

export const STRETCH_CREATE_TIMEOUT_MS = 15_000;
export const STRETCH_COMMAND_TIMEOUT_MS = 5_000;

export const STRETCH_NODE_OPTIONS = Object.freeze({
	numberOfInputs: 1,
	numberOfOutputs: 1,
	outputChannelCount: [2],
	channelCount: 2,
	channelCountMode: 'explicit',
	channelInterpretation: 'speakers'
}) satisfies Readonly<AudioWorkletNodeOptions>;

export interface StretchScheduleChange {
	active?: boolean;
	input?: number;
	rate?: number;
	semitones?: number;
	loopStart?: number;
	loopEnd?: number;
}

export type StrictStretchSchedule = SignalsmithStretchSchedule & { outputTime: number };

export class StretchCommandTimeoutError extends Error {
	constructor(operation: string, timeoutMs: number) {
		super(`Signalsmith ${operation} timed out after ${timeoutMs}ms`);
		this.name = 'StretchCommandTimeoutError';
	}
}

export class StretchProcessorError extends Error {
	constructor(message: string, options?: ErrorOptions) {
		super(message, options);
		this.name = 'StretchProcessorError';
	}
}

function _assertFiniteNonNegative(name: string, value: number): void {
	if (!Number.isFinite(value) || value < 0) {
		throw new RangeError(`${name} must be a finite non-negative number, got ${value}`);
	}
}

export function buildStretchSchedule(
	outputTime: number,
	change: StretchScheduleChange
): StrictStretchSchedule {
	_assertFiniteNonNegative('output time', outputTime);
	if (change.input !== undefined) _assertFiniteNonNegative('input time', change.input);
	if (change.rate !== undefined && (!Number.isFinite(change.rate) || change.rate <= 0)) {
		throw new RangeError(`rate must be a finite positive number, got ${change.rate}`);
	}
	if (change.semitones !== undefined && !Number.isFinite(change.semitones)) {
		throw new RangeError(`semitones must be finite, got ${change.semitones}`);
	}
	if (change.loopStart !== undefined) _assertFiniteNonNegative('loopStart', change.loopStart);
	if (change.loopEnd !== undefined) _assertFiniteNonNegative('loopEnd', change.loopEnd);
	if (
		change.loopStart !== undefined &&
		change.loopEnd !== undefined &&
		change.loopStart >= change.loopEnd
	) {
		throw new RangeError(
			`loopStart must be less than loopEnd, got ${change.loopStart}..${change.loopEnd}`
		);
	}
	return { output: outputTime, outputTime, ...change };
}

export async function withStretchCommandTimeout<T>(
	command: Promise<T>,
	operation: string,
	timeoutMs = STRETCH_COMMAND_TIMEOUT_MS
): Promise<T> {
	if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
		throw new RangeError(`timeoutMs must be a finite positive number, got ${timeoutMs}`);
	}
	let timer: ReturnType<typeof setTimeout> | undefined;
	try {
		return await Promise.race([
			command,
			new Promise<never>((_resolve, reject) => {
				timer = setTimeout(
					() => reject(new StretchCommandTimeoutError(operation, timeoutMs)),
					timeoutMs
				);
			})
		]);
	} finally {
		if (timer !== undefined) clearTimeout(timer);
	}
}

export function stretchProcessorError(event: Event): StretchProcessorError {
	const error = 'error' in event && event.error instanceof Error ? event.error : undefined;
	const detail =
		'message' in event && typeof event.message === 'string' && event.message.length > 0
			? `: ${event.message}`
			: '';
	return new StretchProcessorError(`Signalsmith processor failed during audio rendering${detail}`, {
		cause: error
	});
}

export interface StretchAdapterOptions {
	onInputTime?: (inputTimeSec: number) => void;
	onProcessorError: (error: StretchProcessorError) => void;
	commandTimeoutMs?: number;
}

/** One terminal-failure-aware worklet instance for one deck. */
export class StretchDeckProcessor {
	readonly #node: SignalsmithStretchNode;
	readonly #commandTimeoutMs: number;
	#terminalError: StretchProcessorError | StretchCommandTimeoutError | null = null;

	private constructor(node: SignalsmithStretchNode, options: StretchAdapterOptions) {
		this.#node = node;
		this.#commandTimeoutMs = options.commandTimeoutMs ?? STRETCH_COMMAND_TIMEOUT_MS;
		if (!Number.isFinite(this.#commandTimeoutMs) || this.#commandTimeoutMs <= 0) {
			throw new RangeError(
				`commandTimeoutMs must be a finite positive number, got ${this.#commandTimeoutMs}`
			);
		}
		this.#node.addEventListener('processorerror', (event) => {
			const error = stretchProcessorError(event);
			this.#terminalError = error;
			options.onProcessorError(error);
		});
	}

	static async create(
		context: AudioContext,
		options: StretchAdapterOptions
	): Promise<StretchDeckProcessor> {
		if (context.audioWorklet === undefined) {
			throw new StretchProcessorError('AudioWorklet is unavailable; Signalsmith cannot start');
		}
		const node = await withStretchCommandTimeout(
			createSignalsmithStretch(context, STRETCH_NODE_OPTIONS),
			'processor creation',
			STRETCH_CREATE_TIMEOUT_MS
		);
		const processor = new StretchDeckProcessor(node, options);
		const onInputTime = options.onInputTime;
		if (onInputTime !== undefined) {
			await processor.#command(
				() => node.setUpdateInterval(1 / 30, onInputTime),
				'position update configuration'
			);
		}
		return processor;
	}

	connect(destination: AudioNode): void {
		this.#assertOperational();
		this.#node.connect(destination);
	}

	disconnect(): void {
		this.#node.disconnect();
	}

	async load(buffer: AudioBuffer): Promise<void> {
		this.#assertOperational();
		if (buffer.numberOfChannels < 1 || buffer.numberOfChannels > 2) {
			throw new RangeError(
				`Signalsmith decks require mono or stereo audio, got ${buffer.numberOfChannels} channels`
			);
		}
		const channels = Array.from({ length: buffer.numberOfChannels }, (_unused, channel) => {
			const samples = new Float32Array(buffer.length);
			buffer.copyFromChannel(samples, channel);
			return samples;
		});
		await this.#command(() => this.#node.dropBuffers(), 'drop buffers');
		const transfer = channels.map((channel) => channel.buffer);
		await this.#command(() => this.#node.addBuffers(channels, transfer), 'add buffers');
	}

	async latencySec(): Promise<number> {
		const latency = await this.#command(() => this.#node.latency(), 'latency query');
		_assertFiniteNonNegative('Signalsmith latency', latency);
		return latency;
	}

	async schedule(outputTime: number, change: StretchScheduleChange): Promise<void> {
		await this.#command(
			() => this.#node.schedule(buildStretchSchedule(outputTime, change)),
			'schedule'
		);
	}

	async stop(outputTime: number): Promise<void> {
		await this.schedule(outputTime, { active: false });
	}

	#assertOperational(): void {
		if (this.#terminalError !== null) throw this.#terminalError;
	}

	async #command<T>(command: () => Promise<T>, operation: string): Promise<T> {
		this.#assertOperational();
		try {
			const result = await withStretchCommandTimeout(
				command(),
				operation,
				this.#commandTimeoutMs
			);
			this.#assertOperational();
			return result;
		} catch (error) {
			if (
				this.#terminalError === null &&
				(error instanceof StretchProcessorError || error instanceof StretchCommandTimeoutError)
			) {
				this.#terminalError = error;
			}
			if (this.#terminalError !== null) throw this.#terminalError;
			throw error;
		}
	}
}
