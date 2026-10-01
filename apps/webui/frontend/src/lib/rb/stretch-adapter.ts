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
 *   ✔︎ Transfer mono/stereo PCM into the processor: copied by default, moved
 *     when the caller gives the buffer up (stretch-pcm-handoff.ts).
 *     [if] decoded audio has more than two channels [then] loading rejects
 *     [if] a load asks for 'transfer' on an engine that can move channels
 *       [then] no channel is copied and the load reports 'transfer'
 *   ✔︎ Retiring a deck terminates its source worklet and releases transferred PCM.
 *     [if] dispose completes [then] the port is closed and later commands fail
 */

import type {
	SignalsmithStretchNode,
	SignalsmithStretchSchedule
} from 'signalsmith-stretch';
import { recordWorkletAck } from '$lib/rb/worklet-ack-stats';
import { pcmChannelsForWorklet, probeChannelTransfer, type PcmHandoff } from './stretch-pcm-handoff';
import {
	STRETCH_COMMAND_TIMEOUT_MS,
	STRETCH_CREATE_TIMEOUT_MS,
	StretchProcessorError,
	withStretchCommandTimeout
} from './stretch-errors';

/**
 * LATENCY round 2, STEP 2: the STFT block length to configure every processor
 * with, or `null` to leave the library on its own default.
 *
 * SHIPS AS `null`, which is byte-identical to the behaviour before round 2:
 * with no `configure()` call the vendored library takes its `{preset:
 * 'default'}` branch, giving a 120ms block at 44100Hz.
 *
 * Setting it to 30 is prepared and measured but NOT enabled. The mechanism:
 * `latency()` equals the block exactly (verified against the real library: a
 * `configure({blockMs: 30})` makes `latency()` report 30.00ms), and the onset
 * ramp is 0.39x the block, so the schedule floor (ramp lead + 8ms immediate
 * safety) moves 54.8ms -> 19.7ms, with a measured onset lateness of 0.48ms.
 * The cost is real and is why the flag is off: a 30ms window at 44100Hz
 * resolves ~33Hz per bin against ~8Hz today, i.e. four times worse frequency
 * resolution, which is the direction a phase-vocoder is most sensitive in
 * (bass and tonal stability). That trade is gated on the cross-lane quality
 * methodology, not on this file.
 *
 * GATE EVALUATED Tue 1 Sep 2026, and 30 was REFUSED. Against the committed
 * 162-cell table (`ops/quality/stretch/report.md`) `blockMs 30` is worse than
 * baseline on all three ranking metrics and at all nine conditions: mean LSD
 * 3.06 -> 4.30dB, worst 5.03 -> 5.74dB, and unity LSD (the one disqualifier
 * with a stated definition, D1) 1.94 -> 3.97dB. No joint threshold exists yet
 * (D2-D7 read DEFINITIONS UNAVAILABLE), so the gate cannot be passed, only
 * asserted. `blockMs 60` is the surviving candidate at a 31.4ms floor for
 * +0.19dB mean LSD, still gated on a joint D1 threshold and the maintainer's blind A/B.
 * Full evaluation: `.planning/latency-round2-design.md` appendix E.
 *
 * THREE rules if it is ever flipped:
 *  1. GLOBAL ONLY. Every deck and every stem branch must get the same block, or
 *     participants have different onset ramps and a beat-sync group's first
 *     beat smears (`_assertUniformProcessorBlock`, `assertStretchBlockApplied`).
 *  2. CREATE TIME ONLY. Reconfiguring a PLAYING deck was measured to drop its
 *     output to effective silence (-33 to -50dB) for 10-45ms. Before `load()`
 *     no audio exists, so that hole cannot be punched.
 *  3. REFRESH THE CACHES. `rt.latencySec` and `AlignedStemDeckProcessor`'s own
 *     copy are both snapshots; `latency()` updates truthfully after configure,
 *     but only if somebody re-reads it.
 *
 * `.planning/latency-round2-design.md` section 4 has the block-by-block table.
 */
export const STRETCH_BLOCK_MS: number | null = null;

/**
 * Tolerance for "the configured block took". `latency()` quantises the block to
 * whole samples (blockMs 5 reports 5.011ms at 44100Hz = 221 samples), so exact
 * equality would be brittle; 0.05ms is about two samples at 44100Hz and far
 * tighter than any block size we would choose between.
 */
export const STRETCH_BLOCK_TOLERANCE_MS = 0.05;

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

export const STRETCH_RESET_CHANGE: Readonly<Required<StretchScheduleChange>> = Object.freeze({
	active: false,
	input: 0,
	rate: 1,
	semitones: 0,
	loopStart: 0,
	loopEnd: 0
});

export type StrictStretchSchedule = SignalsmithStretchSchedule & { outputTime: number };

/** Command gate shared by every worklet call. A failed or timed-out command
 * poisons the processor, and the poison check runs before the next callback
 * can post another MessagePort request. */
export class StretchCommandGate {
	#terminalError: Error | null = null;

	poison(error: Error): void {
		this.#terminalError = error;
	}

	assertOperational(): void {
		if (this.#terminalError !== null) throw this.#terminalError;
	}

	/**
	 * Q2: this is the ONE choke point every worklet MessagePort round trip
	 * passes through, so it is where the ack distribution is measured. Both
	 * exits record: a timeout that never reached the window would leave the
	 * distribution blind to the exact cliff it exists to watch approaching.
	 * The recording itself is an aggregate write - no ring row per command.
	 */
	async run<T>(operation: string, command: () => Promise<T>, timeoutMs?: number): Promise<T> {
		this.assertOperational();
		const startedMs = performance.now();
		try {
			const acknowledged = await withStretchCommandTimeout(command(), operation, timeoutMs);
			recordWorkletAck(operation, performance.now() - startedMs, true);
			return acknowledged;
		} catch (error) {
			recordWorkletAck(operation, performance.now() - startedMs, false);
			this.poison(error instanceof Error ? error : new Error(String(error)));
			throw error;
		}
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
	const allowedKeys = new Set(['active', 'input', 'rate', 'semitones', 'loopStart', 'loopEnd']);
	for (const key of Object.keys(change)) {
		if (!allowedKeys.has(key)) throw new TypeError(`unknown Signalsmith schedule field: ${key}`);
	}
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
		change.loopStart > change.loopEnd
	) {
		throw new RangeError(
			`loopStart must not exceed loopEnd, got ${change.loopStart}..${change.loopEnd}`
		);
	}
	return {
		output: outputTime,
		outputTime,
		...(change.active === undefined ? {} : { active: change.active }),
		...(change.input === undefined ? {} : { input: change.input }),
		...(change.rate === undefined ? {} : { rate: change.rate }),
		...(change.semitones === undefined ? {} : { semitones: change.semitones }),
		...(change.loopStart === undefined ? {} : { loopStart: change.loopStart }),
		...(change.loopEnd === undefined ? {} : { loopEnd: change.loopEnd })
	};
}

/**
 * The configured block must be the block the processor actually adopted.
 *
 * A `configure()` that silently no-ops on ONE stem branch is caught by the
 * existing cross-branch latency assertion. A `configure()` that no-ops on ALL
 * of them is not: every branch agrees, at the wrong value, and the deck runs a
 * different block from the rest of the fleet with a schedule floor sized for
 * neither. This is the check with teeth for that case, and it runs per
 * processor, at creation, before any audio exists.
 */
export function assertStretchBlockApplied(latencySec: number, blockMs: number): void {
	_assertFiniteNonNegative('Signalsmith latency', latencySec);
	if (!Number.isFinite(blockMs) || blockMs <= 0) {
		throw new RangeError(`blockMs must be a finite positive number, got ${blockMs}`);
	}
	const reportedMs = latencySec * 1000;
	if (Math.abs(reportedMs - blockMs) > STRETCH_BLOCK_TOLERANCE_MS) {
		throw new StretchProcessorError(
			`Signalsmith block configuration did not take: asked for ${blockMs}ms, processor ` +
				`reports ${reportedMs}ms. Block configuration must be identical on every deck ` +
				'and every stem branch - a mixed fleet gives participants different onset ramps, ' +
				'so a beat-sync group launch smears its first beat.'
		);
	}
}

export function assertStretchLoadIsFresh(loadedDurationSec: number): void {
	if (loadedDurationSec > 0) {
		throw new StretchProcessorError(
			'Signalsmith deck processors are one-shot; create a new processor to replace audio'
		);
	}
}

export function validateStretchBufferMetadata(
	buffer: Pick<AudioBuffer, 'duration' | 'numberOfChannels' | 'sampleRate'>,
	contextSampleRateHz: number
): void {
	if (buffer.sampleRate !== contextSampleRateHz) {
		throw new RangeError(
			`decoded sample rate ${buffer.sampleRate}Hz does not match AudioContext ` +
				`${contextSampleRateHz}Hz`
		);
	}
	if (!Number.isFinite(buffer.duration) || buffer.duration <= 0) {
		throw new RangeError(`decoded buffer duration must be positive, got ${buffer.duration}`);
	}
	if (buffer.numberOfChannels < 1 || buffer.numberOfChannels > 2) {
		throw new RangeError(
			`Signalsmith decks require mono or stereo audio, got ${buffer.numberOfChannels} channels`
		);
	}
}

export function validateStretchScheduleBounds(
	change: StretchScheduleChange,
	loadedDurationSec: number,
	loadedSampleRateHz: number,
	contextSampleRateHz: number
): void {
	if (change.active === true && loadedDurationSec <= 0) {
		throw new StretchProcessorError('Signalsmith cannot start before audio buffers are loaded');
	}
	if (change.active === true && loadedSampleRateHz !== contextSampleRateHz) {
		throw new StretchProcessorError(
			`loaded sample rate ${loadedSampleRateHz}Hz no longer matches AudioContext ` +
				`${contextSampleRateHz}Hz`
		);
	}
	if (change.input !== undefined && (change.input < 0 || change.input > loadedDurationSec)) {
		throw new RangeError(
			`input ${change.input}s is outside loaded audio 0..${loadedDurationSec}s`
		);
	}
	if (change.loopEnd !== undefined && change.loopEnd > loadedDurationSec) {
		throw new RangeError(
			`loopEnd ${change.loopEnd}s exceeds loaded audio ${loadedDurationSec}s`
		);
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
	readonly #context: AudioContext;
	readonly #gate = new StretchCommandGate();
	readonly #node: SignalsmithStretchNode;
	readonly #commandTimeoutMs: number;
	readonly #processorErrorListener: EventListener;
	#loadedDurationSec = 0;
	#loadedSampleRateHz = 0;
	#disposed = false;

	private constructor(
		context: AudioContext,
		node: SignalsmithStretchNode,
		options: StretchAdapterOptions
	) {
		this.#context = context;
		this.#node = node;
		this.#commandTimeoutMs = options.commandTimeoutMs ?? STRETCH_COMMAND_TIMEOUT_MS;
		if (!Number.isFinite(this.#commandTimeoutMs) || this.#commandTimeoutMs <= 0) {
			throw new RangeError(
				`commandTimeoutMs must be a finite positive number, got ${this.#commandTimeoutMs}`
			);
		}
		this.#processorErrorListener = (event) => {
			const error = stretchProcessorError(event);
			this.#gate.poison(error);
			options.onProcessorError(error);
		};
		this.#node.addEventListener('processorerror', this.#processorErrorListener);
	}

	static async create(
		context: AudioContext,
		options: StretchAdapterOptions
	): Promise<StretchDeckProcessor> {
		const { ensureStretchWorkletReady } = await import('./stretch-worklet-ready');
		const createSignalsmithStretch = await ensureStretchWorkletReady(context);
		// Q2: the ready handshake is the one worklet round trip that bypasses
		// StretchCommandGate.run, and it is both the slowest (a measured ~1.35s
		// across four serialized stem creates) and the owner of the separate
		// 15000ms cliff. Timed here so it is not the single unmeasured command.
		const createStartedMs = performance.now();
		let node: SignalsmithStretchNode;
		try {
			node = await withStretchCommandTimeout(
				createSignalsmithStretch(context, STRETCH_NODE_OPTIONS),
				'worklet ready handshake',
				STRETCH_CREATE_TIMEOUT_MS
			);
		} catch (error) {
			recordWorkletAck('processor creation', performance.now() - createStartedMs, false);
			throw error;
		}
		recordWorkletAck('processor creation', performance.now() - createStartedMs, true);
		const processor = new StretchDeckProcessor(context, node, options);
		// STEP 2, off by default. Deliberately here and nowhere else: this is the
		// only moment a processor exists with no audio loaded and nothing
		// scheduled, and reconfiguring later was measured to silence a playing
		// deck for 10-45ms. `configure()` re-reads both latency terms from WASM,
		// so the check below reads the value the processor actually adopted.
		const blockMs = STRETCH_BLOCK_MS;
		if (blockMs !== null) {
			await processor.#command(() => node.configure({ blockMs }), 'block configuration');
			assertStretchBlockApplied(await processor.latencySec(), blockMs);
		}
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

	/** Permanently retire this one-shot processor: disconnect first for
	 * synchronous silence, then let the patched worklet transfer retained PCM
	 * back and mark itself finished so WebKit can collect it. Bypasses the
	 * poisoned command gate so a failed processor can still release memory. */
	async dispose(): Promise<void> {
		if (this.#disposed) return;
		this.#disposed = true;
		this.#node.disconnect();
		this.#node.removeEventListener('processorerror', this.#processorErrorListener);
		try {
			await withStretchCommandTimeout(
				this.#node.dispose(),
				'processor disposal',
				this.#commandTimeoutMs
			);
		} finally {
			this.#node.port.onmessage = null;
			this.#node.port.close();
			this.#loadedDurationSec = 0;
			this.#loadedSampleRateHz = 0;
		}
	}

	/**
	 * `'copy'` leaves `buffer` usable. `'transfer'` GIVES IT UP: its channels
	 * are detached and read as empty afterwards, so only a caller with no
	 * later reader may ask for it. Resolves to what actually happened.
	 */
	async load(buffer: AudioBuffer, requested: PcmHandoff = 'copy'): Promise<PcmHandoff> {
		this.#assertOperational();
		assertStretchLoadIsFresh(this.#loadedDurationSec);
		validateStretchBufferMetadata(buffer, this.#context.sampleRate);
		const { channels, handoff } = pcmChannelsForWorklet(
			buffer,
			requested,
			requested === 'transfer' && _engineMovesChannels(this.#context)
		);
		await this.schedule(this.#context.currentTime, STRETCH_RESET_CHANGE);
		await this.#command(() => this.#node.dropBuffers(), 'drop buffers');
		const transfer = channels.map((channel) => channel.buffer);
		const loadedEndSec = await this.#command(
			() => this.#node.addBuffers(channels, transfer),
			'add buffers'
		);
		if (!Number.isFinite(loadedEndSec) || loadedEndSec <= 0) {
			throw new StretchProcessorError(
				`Signalsmith reported invalid loaded duration ${loadedEndSec}`
			);
		}
		this.#loadedDurationSec = loadedEndSec;
		this.#loadedSampleRateHz = buffer.sampleRate;
		return handoff;
	}

	async latencySec(): Promise<number> {
		const latency = await this.#command(() => this.#node.latency(), 'latency query');
		_assertFiniteNonNegative('Signalsmith latency', latency);
		return latency;
	}

	async schedule(outputTime: number, change: StretchScheduleChange): Promise<void> {
		this.#assertOperational();
		validateStretchScheduleBounds(
			change,
			this.#loadedDurationSec,
			this.#loadedSampleRateHz,
			this.#context.sampleRate
		);
		await this.#command(
			() => this.#node.schedule(buildStretchSchedule(outputTime, change)),
			'schedule'
		);
	}

	async stop(outputTime: number): Promise<void> {
		await this.schedule(outputTime, { active: false });
	}

	#assertOperational(): void {
		if (this.#disposed) throw new StretchProcessorError('Signalsmith processor is disposed');
		this.#gate.assertOperational();
	}

	async #command<T>(command: () => Promise<T>, operation: string): Promise<T> {
		return this.#gate.run(operation, command, this.#commandTimeoutMs);
	}
}

let _channelMoveVerdict: boolean | null = null;

/** Probed once per page: an engine property, not a context property. */
function _engineMovesChannels(context: AudioContext): boolean {
	_channelMoveVerdict ??= probeChannelTransfer(
		() => context.createBuffer(1, 8, context.sampleRate),
		typeof structuredClone === 'function'
			? (view) => structuredClone(view, { transfer: [view.buffer] })
			: null
	);
	return _channelMoveVerdict;
}

export { ensureStretchWorkletReady, resetStretchWorkletReadyForTests } from './stretch-worklet-ready';
