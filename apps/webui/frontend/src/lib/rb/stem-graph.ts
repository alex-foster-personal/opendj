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

import { decodeStemParts, stemDecodeLabels } from '$lib/player/decode/flac-stem-decode';
import { awaitEagerStemDecodeSlot } from '$lib/rb/stem-decode-shed';
import { assertUnitRange, stemLinearFromKnob } from '$lib/player/constants';
import { processorOnsetLeadSec } from '$lib/player/transport/schedule-math';
import {
	StretchDeckProcessor,
	type StretchAdapterOptions,
	type StretchScheduleChange
} from '$lib/rb/stretch-adapter';
import type {
	StemAlignment,
	StemControl,
	StemControlState,
	StemDeckState,
	StemLoadDetail,
	StemLayout
} from '$lib/rb/stem-types';

export const STEM_CONTROLS: readonly StemControl[] = ['vocal', 'instrumental', 'drums'];
export const DEMUCS_PARTS = ['vocals', 'drums', 'bass', 'other'] as const;
export type DemucsPart = (typeof DEMUCS_PARTS)[number];

export const ROFORMER_PARTS = ['vocals', 'instrumental'] as const;

/** Every part name any layout can use. */
export type StemPart = DemucsPart | 'instrumental';

export const STEM_LAYOUT_PARTS: Record<StemLayout, readonly StemPart[]> = {
	demucs4: DEMUCS_PARTS,
	roformer2: ROFORMER_PARTS
};

/** Controls a layout can genuinely drive.
 *
 * roformer2 omits `drums` on purpose. Its `instrumental` part already contains
 * the drums, so there is no drums signal to gain to zero -- a DRUMS button
 * here could only ever do nothing, and a button that does nothing is worse
 * than a button that is visibly unavailable. */
export const STEM_LAYOUT_CONTROLS: Record<StemLayout, readonly StemControl[]> = {
	demucs4: ['vocal', 'instrumental', 'drums'],
	roformer2: ['vocal', 'instrumental']
};

export type StemControls = Record<StemControl, StemControlState>;
export type StemBuffers = Partial<Record<StemPart, AudioBuffer>>;

export interface StemArtifactIdentity {
	source: 'demucs' | 'roformer';
	model: string;
	layout: StemLayout;
}

/** The layout whose part set exactly matches these buffers. Throws rather than
 * guessing: a buffer set matching no known layout is a contract breach. */
export function layoutOfBuffers(buffers: StemBuffers): StemLayout {
	const keys = Object.keys(buffers).sort().join(',');
	for (const [layout, parts] of Object.entries(STEM_LAYOUT_PARTS)) {
		if ([...parts].sort().join(',') === keys) return layout as StemLayout;
	}
	throw new TypeError(`stem buffers {${keys}} match no known layout`);
}

function _exactKeys(name: string, value: object, keys: readonly string[]): void {
	const actual = Object.keys(value).sort();
	const expected = [...keys].sort();
	if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
		throw new TypeError(`${name} must contain exactly ${expected.join(', ')}`);
	}
}

/**
 * Q18 rung 1: encoded parts in, decoded StemBuffers out, four workers deep.
 *
 * Lives here rather than beside the decoder because this is the boundary the
 * engine already knows - `stem-graph` is what turns parts into a graph, and
 * `audio-engine.svelte.ts` sits at exactly its `frontend.max_fan_out` ratchet
 * ceiling and can absorb no new module import. The rung, its three refusals
 * and its numbers live in `$lib/player/decode/flac-stem-decode`.
 */
export async function decodeStemBuffers(
	ctx: BaseAudioContext,
	encoded: Partial<Record<StemPart, ArrayBuffer>>,
	parts: readonly StemPart[],
	hooks: { deck?: number; stale?: () => boolean; onDeferred?: () => void; onStart?: () => void } = {}
): Promise<{ buffers: StemBuffers; labels: Record<string, string> }> {
	// PERFMODE-04 (eager-stem-decode): the deck is already playable on its mix
	// buffer at this point, so yielding here under pressure never blocks audio.
	// STEM-47: the hold is bounded and named on the deck (`onDeferred`).
	const waitStartedMs = performance.now();
	const decodeStart = await awaitEagerStemDecodeSlot({
		...(hooks.deck === undefined ? {} : { deck: hooks.deck }),
		...(hooks.stale === undefined ? {} : { stale: hooks.stale }),
		...(hooks.onDeferred === undefined ? {} : { onDeferred: hooks.onDeferred })
	});
	const decodeWaitMs = Math.round(performance.now() - waitStartedMs);
	hooks.onStart?.();
	const decoded = await decodeStemParts(ctx, encoded, parts);
	return {
		buffers: decoded.buffers,
		labels: {
			...stemDecodeLabels(decoded.reports),
			decode_start: decodeStart,
			decode_wait_ms: String(decodeWaitMs)
		}
	};
}

export function createDefaultStemControls(): StemControls {
	return {
		vocal: { muted: false, solo: false, gain: 0.5 },
		instrumental: { muted: false, solo: false, gain: 0.5 },
		drums: { muted: false, solo: false, gain: 0.5 }
	};
}

export function unavailableStemDeckState(error: string | null = null): StemDeckState {
	return {
		status: 'unavailable',
		source: null,
		model: null,
		layout: null,
		available_controls: [],
		alignment: null,
		controls: createDefaultStemControls(),
		error,
		load: null
	};
}

/** Secondary stem load is in flight; the deck is ALREADY playable on its mix
 * buffer. Distinct from `unavailable` on purpose: `unavailable` is a settled
 * answer (this track has no bundle), `loading` is "not settled yet", and only
 * the second one justifies an on-deck spinner. Controls stay empty so a stem
 * button cannot be armed against a processor that has not arrived. */
export function loadingStemDeckState(
	load: StemLoadDetail = { phase: 'probing', progress: null, reason: null }
): StemDeckState {
	return {
		status: 'loading',
		source: null,
		model: null,
		layout: null,
		available_controls: [],
		alignment: null,
		controls: createDefaultStemControls(),
		error: null,
		load: { ...load, progress: load.progress === null ? null : { ...load.progress } }
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
		layout: identity.layout,
		available_controls: [...STEM_LAYOUT_CONTROLS[identity.layout]],
		alignment: { ...alignment },
		controls: createDefaultStemControls(),
		error: null,
		load: null
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
		_exactKeys(`stem controls.${stem}`, state, ['muted', 'solo', 'gain']);
		if (typeof state.muted !== 'boolean' || typeof state.solo !== 'boolean') {
			throw new TypeError(`stem controls.${stem} mute/solo values must be boolean`);
		}
		assertUnitRange(`stem controls.${stem}.gain`, state.gain);
	}
}

export function stemPartGains(
	controls: StemControls,
	layout: StemLayout = 'demucs4'
): Partial<Record<StemPart, number>> {
	_validateControls(controls);
	// Solo is evaluated over the controls this LAYOUT owns. Counting a soloed
	// DRUMS on a roformer2 deck would silence both real parts and play nothing
	// -- a control the layout cannot drive must not be able to mute the deck.
	const owned = STEM_LAYOUT_CONTROLS[layout];
	const anySolo = owned.some((stem) => controls[stem].solo);
	const gain = (stem: StemControl): number => {
		const state = controls[stem];
		const gate = !state.muted && (!anySolo || state.solo) ? 1 : 0;
		return gate * stemLinearFromKnob(state.gain);
	};
	if (layout === 'roformer2') {
		return { vocals: gain('vocal'), instrumental: gain('instrumental') };
	}
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
	const parts = STEM_LAYOUT_PARTS[layoutOfBuffers(buffers)];
	// `vocals` is in every layout, so it is always a valid reference.
	const reference = buffers.vocals as AudioBuffer;
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
	for (const part of parts) {
		const buffer = buffers[part] as AudioBuffer;
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
	processors: Partial<Record<StemPart, SchedulableStemProcessor>>,
	outputTime: number,
	change: StretchScheduleChange,
	layout: StemLayout = 'demucs4'
): Promise<void> {
	const parts = STEM_LAYOUT_PARTS[layout];
	_exactKeys('stem processors', processors, parts);
	const outcomes = await Promise.allSettled(
		parts.map((part) => (processors[part] as SchedulableStemProcessor).schedule(outputTime, change))
	);
	_aggregateFailures('schedule', outcomes);
}

/** Four real time-stretch processors sharing exact schedules and summing into
 * the existing deck analyser. No original-mix bypass exists once selected. */
export class AlignedStemDeckProcessor {
	readonly #context: AudioContext;
	readonly #processors: Partial<Record<StemPart, StretchDeckProcessor>>;
	readonly #gains: Partial<Record<StemPart, GainNode>>;
	readonly #latencySec: number;
	readonly #layout: StemLayout;
	readonly #parts: readonly StemPart[];
	#controls: StemControls = createDefaultStemControls();

	private constructor(
		context: AudioContext,
		processors: Partial<Record<StemPart, StretchDeckProcessor>>,
		gains: Partial<Record<StemPart, GainNode>>,
		latencySec: number,
		layout: StemLayout
	) {
		this.#context = context;
		this.#processors = processors;
		this.#gains = gains;
		this.#latencySec = latencySec;
		this.#layout = layout;
		this.#parts = STEM_LAYOUT_PARTS[layout];
	}

	get layout(): StemLayout {
		return this.#layout;
	}

	/** Controls this deck can drive; the rest must render inert. */
	get availableControls(): readonly StemControl[] {
		return STEM_LAYOUT_CONTROLS[this.#layout];
	}

	static async create(
		context: AudioContext,
		buffers: StemBuffers,
		options: StretchAdapterOptions
	): Promise<{ processor: AlignedStemDeckProcessor; alignment: StemAlignment }> {
		const alignment = validateStemBufferAlignment(buffers);
		const layout = layoutOfBuffers(buffers);
		const parts = STEM_LAYOUT_PARTS[layout];
		const processors: Partial<Record<StemPart, StretchDeckProcessor>> = {};
		const gains: Partial<Record<StemPart, GainNode>> = {};
		try {
			for (const part of parts) {
				const processor = await StretchDeckProcessor.create(context, options);
				await processor.load(buffers[part] as AudioBuffer);
				processors[part] = processor;
				const gain = context.createGain();
				gain.gain.value = 1;
				processor.connect(gain);
				gains[part] = gain;
			}
			const latencies = await Promise.all(
				parts.map((part) => (processors[part] as StretchDeckProcessor).latencySec())
			);
			const latencySec = latencies[0];
			if (latencies.some((latency) => Math.abs(latency - latencySec) > 1 / alignment.sample_rate_hz)) {
				throw new Error(`stem processor latency alignment mismatch: ${latencies.join(', ')}`);
			}
			// LATENCY round 2. Equal REPORTED latency is not the property the
			// transport floor consumes: the schedule lead is derived from the ONSET
			// RAMP, and branches that reach full level at different times are a
			// moving comb filter on their own sum, which no amount of schedule
			// alignment fixes. Today the lead is a pure function of the latency, so
			// this cannot fail while the check above passes - it is here so a future
			// ramp source (a measured ramp, or a per-branch block) cannot quietly
			// break the invariant while the older assertion still reads green.
			const leads = latencies.map((latency) => processorOnsetLeadSec(latency));
			if (leads.some((lead) => Math.abs(lead - leads[0]) > 1 / alignment.sample_rate_hz)) {
				throw new Error(
					`stem processor onset-lead mismatch: ${leads.join(', ')}; branches with ` +
						'different onset ramps reach full level at different times, so the summed ' +
						'output combs on every start'
				);
			}
			return {
				processor: new AlignedStemDeckProcessor(
					context, processors, gains, latencySec, layout
				),
				alignment
			};
		} catch (error) {
			const cleanupFailures: unknown[] = [];
			for (const part of parts) {
				try {
					await processors[part]?.dispose();
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
		for (const part of this.#parts) this.#gains[part]?.connect(destination);
	}

	disconnect(): void {
		const failures: unknown[] = [];
		for (const part of this.#parts) {
			try {
				this.#processors[part]?.disconnect();
			} catch (error) {
				failures.push(error);
			}
			try {
				this.#gains[part]?.disconnect();
			} catch (error) {
				failures.push(error);
			}
		}
		if (failures.length === 1) throw failures[0];
		if (failures.length > 1) throw new AggregateError(failures, 'stem graph disconnect failed');
	}

	async dispose(): Promise<void> {
		const failures: unknown[] = [];
		for (const part of this.#parts) {
			try {
				this.#gains[part]?.disconnect();
			} catch (error) {
				failures.push(error);
			}
		}
		const outcomes = await Promise.allSettled(
			this.#parts.map((part) => (this.#processors[part] as StretchDeckProcessor).dispose())
		);
		for (const outcome of outcomes) {
			if (outcome.status === 'rejected') failures.push(outcome.reason);
		}
		if (failures.length === 1) throw failures[0];
		if (failures.length > 1) throw new AggregateError(failures, 'stem graph disposal failed');
	}

	async latencySec(): Promise<number> {
		return this.#latencySec;
	}

	async schedule(outputTime: number, change: StretchScheduleChange): Promise<void> {
		try {
			await scheduleAlignedStemProcessors(
				this.#processors, outputTime, change, this.#layout
			);
		} catch (error) {
			try {
				await this.dispose();
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
		const gains = stemPartGains(controls, this.#layout);
		for (const part of this.#parts) {
			const value = gains[part];
			const node = this.#gains[part];
			if (value === undefined || node === undefined) {
				throw new Error(`stem gain missing for part ${part} in layout ${this.#layout}`);
			}
			node.gain.setTargetAtTime(value, this.#context.currentTime, 0.01);
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
