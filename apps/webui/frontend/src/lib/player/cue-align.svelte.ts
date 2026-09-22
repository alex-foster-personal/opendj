/**
 * CUEOUT-14: cue/master alignment policy and the calibration state machine.
 *
 * Pure decisions over injected effects. Nothing here touches Web Audio, the
 * mic, the engine or storage: `CueAlignEffects` names every side effect the
 * calibration performs (open the mic, play a chirp train to one bus, record,
 * pause and resume decks, read the clock, persist a result) and the unit
 * tests drive the real decision path with all of them faked. The thin shells
 * that perform them live in player/headphones.ts (audio) and
 * rb/cue-align-session.svelte.ts (deck transport + composition).
 *
 *   idle -> mic_access -> mic_check_master -> mic_check_cue -> measuring -> applied | failed
 *
 * The two `mic_check_*` steps are each one measurement; `measuring` adds two
 * more per bus so every latency is the median of three, and the run is
 * refused when either bus spreads more than `CUE_ALIGN_MAX_SPREAD_MS`.
 * Escape / close / IPC abort at any step raises the AbortSignal every chirp
 * and recording is holding, stops the mic, resumes exactly the decks the run
 * paused, and persists nothing.
 */

import {
	HEAD_DELAY_MAX_MS,
	HEADPHONE_ALIGNMENT_MODES,
	MASTER_DELAY_MAX_MS,
	assertHeadphoneAlignmentMode,
	assertHeadphoneOutputMode,
	type DeckId,
	type HeadphoneAlignmentMode
} from '$lib/player/constants';
import {
	CUE_LATENCY_CLICK_COUNT,
	CUE_LATENCY_CLICK_MS,
	CUE_LATENCY_PEAK_MIN,
	CUE_LATENCY_PERIOD_MS,
	CUE_LATENCY_PREROLL_MS,
	crossCorrelateLagMs,
	cueLatencyCaptureMs,
	cueLatencyClickTrain
	,capturedSignalLevel
} from '$lib/player/cue-latency';
import type { CueCalibrationRecord } from '$lib/player/mixer-config';
import type { CueAlignStep, CueCalibrationFailure, HeadphoneCalibrationState } from '$lib/rb/mixer-types';

export { HEADPHONE_ALIGNMENT_MODES };

/** Three runs per bus; the two mic checks count as run one. */
export const CUE_ALIGN_RUNS = 3;
/** Max-minus-min across the three runs of ONE bus, above which nothing is applied. */
export const CUE_ALIGN_MAX_SPREAD_MS = 10;
/** The longest lag either bus may show; sizes the capture window. */
export const CUE_ALIGN_MAX_LAG_MS = MASTER_DELAY_MAX_MS;

export type CueAlignBus = 'master' | 'cue';

/** What a run writes when it succeeds. `persist` performs all three writes. */
export interface AppliedAlignment {
	head_delay_ms: number;
	master_delay_ms: number;
	record: CueCalibrationRecord;
}

export interface MicHandle {
	stop(): void;
}

export interface CueAlignEffects {
	sampleRate(): number;
	getUserMedia(): Promise<MicHandle>;
	playTrain(bus: CueAlignBus, reference: Float32Array, sampleRate: number, signal: AbortSignal): Promise<void>;
	record(mic: MicHandle, durationMs: number, sampleRate: number, signal: AbortSignal): Promise<Float32Array>;
	sleep(ms: number): Promise<void>;
	/** Pause every playing deck and say which, so resume touches only those. */
	pauseDecks(): Promise<DeckId[]>;
	resumeDecks(decks: readonly DeckId[]): Promise<void>;
	/** Epoch milliseconds, for `measured_at`. */
	now(): number;
	persist(result: AppliedAlignment): void;
	alignmentMode(): HeadphoneAlignmentMode;
	deviceIds(): { cue: string; master: string | null };
}

export interface AlignmentPlan {
	head_delay_ms: number;
	master_delay_ms: number;
	/** Standing copy for the cluster when a mode leaves the phones behind or a cap bit. */
	warning: string | null;
	capped: boolean;
}

function _assertOffsetMs(offsetMs: unknown): asserts offsetMs is number {
	if (typeof offsetMs !== 'number' || !Number.isFinite(offsetMs)) {
		throw new RangeError(`alignment offset must be a finite number of ms, got ${String(offsetMs)}`);
	}
}

/**
 * The mode table from the spec. `offsetMs = cue_latency_ms - master_latency_ms`:
 * negative means the phones are AHEAD (delay the phones), positive BEHIND
 * (delay the room, or in headphones_only warn and delay nothing).
 */
export function deriveAlignment(mode: unknown, offsetMs: unknown): AlignmentPlan {
	assertHeadphoneAlignmentMode(mode);
	_assertOffsetMs(offsetMs);
	const rounded = Math.round(offsetMs);
	if (rounded === 0) return { head_delay_ms: 0, master_delay_ms: 0, warning: null, capped: false };
	if (rounded < 0) {
		const wanted = -rounded;
		const capped = wanted > HEAD_DELAY_MAX_MS;
		return {
			head_delay_ms: Math.min(HEAD_DELAY_MAX_MS, wanted),
			master_delay_ms: 0,
			warning: capped
				? `HEAD DELAY capped at ${HEAD_DELAY_MAX_MS} ms; the phones are ${wanted} ms ahead of the room.`
				: null,
			capped
		};
	}
	if (mode === 'headphones_only') {
		return {
			head_delay_ms: 0,
			master_delay_ms: 0,
			warning: `headphones are ${rounded} ms behind; switch alignment mode to delay the room`,
			capped: false
		};
	}
	const capped = rounded > MASTER_DELAY_MAX_MS;
	return {
		head_delay_ms: 0,
		master_delay_ms: Math.min(MASTER_DELAY_MAX_MS, rounded),
		warning: capped
			? `room delay capped at ${MASTER_DELAY_MAX_MS} ms; the phones are ${rounded} ms behind, so ${rounded - MASTER_DELAY_MAX_MS} ms remain.`
			: null,
		capped
	};
}

/** CALIBRATE is live only in two_outputs with a selected cue sink. The label
 * (wired jack, Bluetooth, anything) is deliberately not consulted. */
export function calibrateButtonEnabled(args: {
	output_mode: unknown;
	selected_output_device_id: string | null;
}): boolean {
	assertHeadphoneOutputMode(args.output_mode);
	if (args.selected_output_device_id !== null && typeof args.selected_output_device_id !== 'string') {
		throw new TypeError('selected headphone output device id must be a string or null');
	}
	return args.output_mode === 'two_outputs' && args.selected_output_device_id !== null;
}

/** Every reason calibration cannot start right now, in the operator's words.
 * The modal, the error the engine raises and the agent-facing commands all
 * read from this one list, so a missing audio graph can never be reported as
 * a missing device. An empty list means calibration is ready to run. */
export function calibrationBlockers(args: {
	audio_graph_ready: unknown;
	output_mode: unknown;
	selected_output_device_id: string | null;
}): string[] {
	if (typeof args.audio_graph_ready !== 'boolean') {
		throw new TypeError('audio_graph_ready must be a boolean');
	}
	const blockers: string[] = [];
	if (!args.audio_graph_ready) {
		blockers.push('the audio graph is not built yet, so load a deck and start playback once');
	}
	if (
		!calibrateButtonEnabled({
			output_mode: args.output_mode,
			selected_output_device_id: args.selected_output_device_id
		})
	) {
		if (args.output_mode !== 'two_outputs') {
			blockers.push(`CUE OUT is ${String(args.output_mode)}, and calibration needs two_outputs`);
		}
		if (args.selected_output_device_id === null) {
			blockers.push('no headphone output device is selected in the I/O pane');
		}
	}
	return blockers;
}

/** Roughly how long a full run takes, for the intro copy. */
export function estimatedCalibrationSeconds(): number {
	const perMeasurementMs = CUE_LATENCY_PREROLL_MS + cueLatencyCaptureMs(CUE_ALIGN_MAX_LAG_MS);
	return Math.round((perMeasurementMs * CUE_ALIGN_RUNS * 2) / 1000);
}

export function median3(values: readonly number[]): number {
	if (values.length !== CUE_ALIGN_RUNS) {
		throw new RangeError(`median expects ${CUE_ALIGN_RUNS} runs, got ${values.length}`);
	}
	return [...values].sort((left, right) => left - right)[1];
}

export function spreadMs(values: readonly number[]): number {
	return Math.max(...values) - Math.min(...values);
}

class _Aborted extends Error {
	constructor() {
		super('cue alignment calibration aborted');
		this.name = 'CueAlignAborted';
	}
}

/** A measurement that failed its peak check. Carries the bus for the copy. */
class _MicCouldNotHear extends Error {
	readonly failure: CueCalibrationFailure;
	constructor(bus: CueAlignBus, peakNormalized: number, inputPeak: number, inputRms: number) {
		const what = bus === 'master' ? 'speakers' : 'headphones';
		if (inputPeak === 0) {
			super(`No input signal was captured while checking the ${what} (input peak 0.000). Check microphone permission and the selected input route; prior delay was kept.`);
			this.name = 'CueAlignNoInputSignal';
			this.failure = 'no_input_signal';
			return;
		}
		super(`The captured input did not correlate with the ${what} probe (correlation ${peakNormalized.toFixed(2)} < ${CUE_LATENCY_PEAK_MIN}; input peak ${inputPeak.toFixed(3)}, rms ${inputRms.toFixed(3)}). Check the selected input and route; prior delay was kept.`);
		this.name = 'CueAlignWeakCorrelation';
		this.failure = 'weak_correlation';
	}
}

export interface CueAlignController {
	/** Resolves when the run reaches applied, failed or was aborted. Rejects only when a run is already in flight. */
	run(opts: { interactive: boolean }): Promise<void>;
	/** The operator has the ear cup on the mic (interactive runs only). */
	continueCueCheck(): void;
	abort(): void;
	running(): boolean;
}

/**
 * Build a controller over `calibration` (the live `mixerState.headphones.calibration`
 * in production, a plain object under test). Every step transition is a write
 * to that object, which is what the mirror publishes and the modal renders.
 */
export function createCueAlignController(
	effects: CueAlignEffects,
	calibration: HeadphoneCalibrationState
): CueAlignController {
	let running = false;
	let abortController: AbortController | null = null;
	let releaseCueCheck: (() => void) | null = null;
	let rejectCueCheck: ((error: Error) => void) | null = null;

	function _setStep(step: CueAlignStep): void {
		calibration.step = step;
	}

	function _throwIfAborted(): void {
		if (abortController?.signal.aborted === true) throw new _Aborted();
	}

	function _resetDiagnostics(): void {
		calibration.diagnostics = {
			probe: 'chirp',
			alternate_probe: 'unavailable',
			failure: null,
			master_measurements_ms: [],
			cue_measurements_ms: [],
			spread_ms: null
		};
	}

	function _recordFailure(failure: CueCalibrationFailure): void {
		calibration.diagnostics.failure = failure;
	}

	async function _measure(bus: CueAlignBus, mic: MicHandle): Promise<number> {
		_throwIfAborted();
		const signal = (abortController as AbortController).signal;
		const sampleRate = effects.sampleRate();
		if (!Number.isFinite(sampleRate) || sampleRate <= 0) {
			throw new RangeError(`calibration sampleRate must be positive, got ${sampleRate}`);
		}
		const reference = cueLatencyClickTrain({
			sampleRate,
			clickMs: CUE_LATENCY_CLICK_MS,
			periodMs: CUE_LATENCY_PERIOD_MS,
			clickCount: CUE_LATENCY_CLICK_COUNT
		});
		const recording = effects.record(mic, cueLatencyCaptureMs(CUE_ALIGN_MAX_LAG_MS), sampleRate, signal);
		// An abort rejects the recording while the chirp is still awaited; the
		// rejection is observed at `await recording` below, never left dangling.
		recording.catch(() => undefined);
		await effects.sleep(CUE_LATENCY_PREROLL_MS);
		await effects.playTrain(bus, reference, sampleRate, signal);
		const captured = await recording;
		_throwIfAborted();
		const input = capturedSignalLevel(captured);
		const { lagMs, peakNormalized } = crossCorrelateLagMs(reference, captured, sampleRate);
		if (!Number.isFinite(peakNormalized) || peakNormalized < CUE_LATENCY_PEAK_MIN) {
			const error = new _MicCouldNotHear(
				bus,
				Number.isFinite(peakNormalized) ? peakNormalized : 0,
				input.peak,
				input.rms
			);
			_recordFailure(error.failure);
			throw error;
		}
		return Math.round(lagMs);
	}

	function _waitForCueCheck(): Promise<void> {
		return new Promise<void>((resolve, reject) => {
			releaseCueCheck = resolve;
			rejectCueCheck = reject;
		}).finally(() => {
			releaseCueCheck = null;
			rejectCueCheck = null;
		});
	}

	async function _runBody(interactive: boolean, mic: MicHandle): Promise<void> {
		const master: number[] = [];
		const cue: number[] = [];
		_setStep('mic_check_master');
		master.push(await _measure('master', mic));
		calibration.diagnostics.master_measurements_ms = [...master];
		calibration.master_latency_ms = master[0];
		_setStep('mic_check_cue');
		if (interactive) {
			await _waitForCueCheck();
			_throwIfAborted();
		}
		cue.push(await _measure('cue', mic));
		calibration.diagnostics.cue_measurements_ms = [...cue];
		calibration.cue_latency_ms = cue[0];
		_setStep('measuring');
		for (let run = 1; run < CUE_ALIGN_RUNS; run += 1) {
			master.push(await _measure('master', mic));
			calibration.diagnostics.master_measurements_ms = [...master];
			cue.push(await _measure('cue', mic));
			calibration.diagnostics.cue_measurements_ms = [...cue];
		}
		const spread = Math.max(spreadMs(master), spreadMs(cue));
		calibration.diagnostics.spread_ms = spread;
		if (spread > CUE_ALIGN_MAX_SPREAD_MS) {
			_recordFailure('inconsistent_measurements');
			throw new Error(`inconsistent measurements (spread ${spread} ms); prior delay was kept. Check the selected routes and capture, then run again.`);
		}
		const masterMs = median3(master);
		const cueMs = median3(cue);
		const offsetMs = cueMs - masterMs;
		const plan = deriveAlignment(effects.alignmentMode(), offsetMs);
		const ids = effects.deviceIds();
		effects.persist({
			head_delay_ms: plan.head_delay_ms,
			master_delay_ms: plan.master_delay_ms,
			record: {
				cue_latency_ms: cueMs,
				master_latency_ms: masterMs,
				measured_at: new Date(effects.now()).toISOString(),
				cue_device_id: ids.cue,
				master_device_id: ids.master
			}
		});
		calibration.master_latency_ms = masterMs;
		calibration.cue_latency_ms = cueMs;
		calibration.offset_ms = offsetMs;
		calibration.error = null;
		calibration.diagnostics.failure = null;
		_setStep('applied');
	}

	async function run(opts: { interactive: boolean }): Promise<void> {
		if (typeof opts.interactive !== 'boolean') throw new TypeError('run({ interactive }) must be boolean');
		if (running) throw new Error('cue alignment calibration is already running');
		running = true;
		abortController = new AbortController();
		calibration.error = null;
		calibration.master_latency_ms = null;
		calibration.cue_latency_ms = null;
		calibration.offset_ms = null;
		_resetDiagnostics();
		_setStep('mic_access');
		let mic: MicHandle | null = null;
		let paused: DeckId[] = [];
		try {
			try {
				mic = await effects.getUserMedia();
			} catch (error) {
				throw new Error(`Microphone access failed: ${error instanceof Error ? error.message : String(error)}`);
			}
			_throwIfAborted();
			paused = await effects.pauseDecks();
			await _runBody(opts.interactive, mic);
		} catch (error) {
			if (error instanceof _Aborted || abortController.signal.aborted) {
				calibration.error = null;
				_setStep('idle');
			} else {
				calibration.error = error instanceof Error ? error.message : String(error);
				if (calibration.diagnostics.failure === null) {
					_recordFailure(calibration.step === 'mic_access' ? 'microphone_access' : 'route_or_operation');
				}
				_setStep('failed');
			}
		} finally {
			mic?.stop();
			if (paused.length > 0) await effects.resumeDecks(paused);
			abortController = null;
			running = false;
		}
	}

	function continueCueCheck(): void {
		if (releaseCueCheck === null) throw new Error('nothing is waiting for the ear-cup check');
		releaseCueCheck();
	}

	function abort(): void {
		if (!running || abortController === null) return;
		abortController.abort();
		rejectCueCheck?.(new _Aborted());
	}

	return { run, continueCueCheck, abort, running: () => running };
}
