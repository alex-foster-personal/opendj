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
 *   idle -> mic_access -> mic_check_master -> mic_check_cue -> measuring -> verifying -> applied | failed
 *
 * Both stages measure against ONE clock. `openCapture` resolves only once the
 * microphone is really delivering frames, and reports the context time its
 * first frame landed on; the chirp is then SCHEDULED at an explicit time on
 * that same clock, so where it should appear in the capture is arithmetic
 * rather than a guess. Measured live Wed 16 Sep 2026, the previous shape (a
 * fresh capture context per measurement, and a 40 ms sleep before an immediate
 * `start()`) reported 174, 112, 121 and 203 ms for the SAME bus in the same
 * minute, at peaks of 0.48 that prove the chirp was heard clearly every time.
 * Nothing acoustic moves 91 ms; what varied was when the new capture context
 * happened to begin. A calibration cannot subtract two numbers like that.
 *
 * Each bus is measured in two stages. The `mic_check_*` step is stage one, a
 * level find: the chirp is replayed up the `CUE_LATENCY_GAIN_STEPS` ladder
 * until the mic actually hears that bus, and the rung that worked is kept.
 * Stage two is `measuring`, which replays two more chirps per bus AT THAT RUNG
 * so every latency is the median of three, and refuses the run when either bus
 * spreads more than `CUE_ALIGN_MAX_SPREAD_MS`. The level find doubles as run
 * one, so a bus the mic hears immediately costs exactly what it used to.
 * Escape / close / IPC abort at any step raises the AbortSignal every chirp
 * and recording is holding, stops the mic, resumes exactly the decks the run
 * paused, and persists nothing.
 */

import type { DeckId, HeadphoneAlignmentMode } from '$lib/player/constants';
import {
	CUE_LATENCY_GAIN_STEPS,
	CUE_LATENCY_PEAK_MIN,
	CUE_LATENCY_PEAK_TARGET,
	CUE_LATENCY_PREROLL_MS,
	crossCorrelateLagMs,
	cueLatencyCaptureMs,
	cueLatencySweep
} from '$lib/player/cue-latency';
import { CUE_ALIGN_MAX_LAG_MS, CUE_ALIGN_RUNS, deriveAlignment, intendedResidualMs } from '$lib/player/cue-align-policy';
import type { CueCalibrationRecord } from '$lib/player/mixer-config';
import type { CueAlignStep, HeadphoneCalibrationState } from '$lib/rb/mixer-types';

/** Max-minus-min across the three runs of ONE bus, above which nothing is applied. */
export const CUE_ALIGN_MAX_SPREAD_MS = 10;
/** Post-apply verification accepts when |cue minus master| is at most this. Same order of
 * magnitude as `CUE_ALIGN_MAX_SPREAD_MS`: confirms alignment within measurement noise. */
export const CUE_ALIGN_VERIFY_TOLERANCE_MS = 10;
/** Below this the capture is room noise correlating with itself, i.e. the bus
 * was never heard at all rather than heard faintly. Measured: a dead acoustic
 * path scores 0.00-0.03, a faint but real one 0.09 upward. */
export const SILENT_PEAK_MAX = 0.05;

export type CueAlignBus = 'master' | 'cue';

/** Whether a chirp measures raw path latency or confirms applied delays. */
export type CueAlignChirpPurpose = 'measure' | 'verify';

/** One rung of the stage-one ramp: the gain tried, what the mic scored, and
 * the lag that attempt measured. */
export interface LevelAttempt {
	gain: number;
	peak: number;
	lagMs: number;
}

/**
 * Stage one's stop rule, as a pure function over what the ramp has heard.
 *
 * Prefers the first rung comfortably above the refusal line, falls back to the
 * best rung that cleared it at all, and returns null when none did. Taking the
 * best rather than the first of the marginal ones matters because the ladder is
 * not monotonic in practice: a bus can score worse at full level than at half,
 * when the extra drive buys distortion rather than signal.
 */
export function chooseLevelRung(attempts: readonly LevelAttempt[]): { gain: number; lagMs: number } | null {
	const clearing = attempts.filter((attempt) => attempt.peak >= CUE_LATENCY_PEAK_MIN);
	if (clearing.length === 0) return null;
	const comfortable = clearing.find((attempt) => attempt.peak >= CUE_LATENCY_PEAK_TARGET);
	const chosen = comfortable ?? clearing.reduce((best, next) => (next.peak > best.peak ? next : best));
	return { gain: chosen.gain, lagMs: chosen.lagMs };
}

/** What a run writes when it succeeds. `persist` performs all three writes. */
export interface AppliedAlignment {
	head_delay_ms: number;
	master_delay_ms: number;
	record: CueCalibrationRecord;
}

export interface MicHandle {
	stop(): void;
}

/** A capture that is already running. `startedAtSec` is the shared-clock time
 * of its FIRST frame, which is what turns a captured position into a latency. */
export interface OpenCapture {
	startedAtSec: number;
	samples: Promise<Float32Array>;
}

export interface CueAlignEffects {
	sampleRate(): number;
	getUserMedia(): Promise<MicHandle>;
	/** Schedule the chirp to BEGIN at `whenSec` on the shared clock. */
	playTrain(
		bus: CueAlignBus,
		reference: Float32Array,
		sampleRate: number,
		signal: AbortSignal,
		gain: number,
		whenSec: number,
		purpose?: CueAlignChirpPurpose
	): Promise<void>;
	/** Open the capture and resolve only once it is really running. */
	openCapture(mic: MicHandle, durationMs: number, sampleRate: number, signal: AbortSignal): Promise<OpenCapture>;
	/** The shared clock the capture and the chirp are both measured against. */
	contextTimeSec(): number;
	sleep(ms: number): Promise<void>;
	/** Pause every playing deck and say which, so resume touches only those. */
	pauseDecks(): Promise<DeckId[]>;
	resumeDecks(decks: readonly DeckId[]): Promise<void>;
	/** Epoch milliseconds, for `measured_at`. */
	now(): number;
	persist(result: AppliedAlignment): void;
	readDelays(): { head_delay_ms: number; master_delay_ms: number };
	setDelays(delays: { head_delay_ms: number; master_delay_ms: number }): void;
	alignmentMode(): HeadphoneAlignmentMode;
	deviceIds(): { cue: string; master: string | null };
	/** Observer, not a decision input: stage one reports each rung it tries and
	 * the peak it got, so the operator can move the ear cup and watch the number
	 * climb instead of waiting for a verdict. Nothing branches on it. */
	onProbe?(probe: CueAlignProbe): void;
}

/** One stage-one attempt. `peak` is null while that rung is still playing. */
export interface CueAlignProbe {
	bus: CueAlignBus;
	gain: number;
	peak: number | null;
	/** What this attempt measured, null while it is still playing. Two attempts
	 * at one rung that agree on peak but not on lag are a clock problem, not an
	 * acoustic one, which is the distinction this field exists to expose. */
	lagMs: number | null;
	best: number;
	threshold: number;
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

/**
 * A bus the mic never heard, at any rung of the gain ladder. Carries the best
 * peak of the whole ramp, and separates the two failures that look identical
 * on screen but have nothing in common:
 *
 * - a peak near zero means NOTHING acoustic arrived. Raising the level cannot
 *   fix that. It is a routing fault (the bus points at a device nobody is
 *   listening to) or an input that returns digital silence.
 * - a peak that climbed but stalled short is genuinely too quiet, and the
 *   remaining lever is the OS output volume, which a page cannot set.
 */
class _MicCouldNotHear extends Error {
	constructor(bus: CueAlignBus, peak: number) {
		const what = bus === 'master' ? 'speakers' : 'headphones';
		const routing =
			bus === 'master'
				? 'the room output really is the speakers and not a headphone jack'
				: 'the headphones really are plugged into the selected cue output';
		const fix =
			peak < SILENT_PEAK_MAX
				? `Nothing at all reached the microphone, so this is not a volume problem. Check that ${routing}, and that the selected input is a live microphone.`
				: bus === 'master'
					? 'Turn the macOS output volume up, or move the laptop closer to the speakers.'
					: 'Hold one ear cup right against the laptop microphone, and turn that output device up in macOS Sound settings: a web page cannot set a device volume itself.';
		super(
			`The mic could not hear the ${what} even at full chirp level ` +
				`(best peak ${peak.toFixed(2)}, needs ${CUE_LATENCY_PEAK_MIN}). ${fix}`
		);
		this.name = 'CueAlignMicCouldNotHear';
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
	let activePreRunDelays: { head_delay_ms: number; master_delay_ms: number } | null = null;
	let verifyDelaysApplied = false;

	function _setStep(step: CueAlignStep): void {
		calibration.step = step;
	}

	function _throwIfAborted(): void {
		if (abortController?.signal.aborted === true) throw new _Aborted();
	}

	/** One chirp at one rung. Reports what it heard; decides nothing. */
	async function _probe(
		bus: CueAlignBus,
		mic: MicHandle,
		gain: number,
		purpose: CueAlignChirpPurpose = 'measure'
	): Promise<{ lagMs: number; peak: number }> {
		_throwIfAborted();
		const signal = (abortController as AbortController).signal;
		const sampleRate = effects.sampleRate();
		if (!Number.isFinite(sampleRate) || sampleRate <= 0) {
			throw new RangeError(`calibration sampleRate must be positive, got ${sampleRate}`);
		}
		const reference = cueLatencySweep({ sampleRate });
		const capture = await effects.openCapture(mic, cueLatencyCaptureMs(CUE_ALIGN_MAX_LAG_MS), sampleRate, signal);
		// An abort rejects the capture while the chirp is still awaited; the
		// rejection is observed at `await capture.samples` below, never dangling.
		capture.samples.catch(() => undefined);
		_throwIfAborted();
		// Read the clock only now: the capture is live, so this preroll is real
		// headroom rather than a bet on how long a context takes to start.
		const whenSec = effects.contextTimeSec() + CUE_LATENCY_PREROLL_MS / 1000;
		await effects.playTrain(bus, reference, sampleRate, signal, gain, whenSec, purpose);
		const captured = await capture.samples;
		_throwIfAborted();
		const { lagMs, peakNormalized } = crossCorrelateLagMs(reference, captured, sampleRate);
		// Where the chirp was SENT, in the capture's own frames. Subtracting it
		// leaves the latency of the path, which is the only part that differs
		// between the two buses.
		const scheduledMs = (whenSec - capture.startedAtSec) * 1000;
		return {
			lagMs: Math.round(lagMs - scheduledMs),
			peak: Number.isFinite(peakNormalized) ? peakNormalized : 0
		};
	}

	/**
	 * Stage one. Climb the gain ladder until the mic hears this bus, and hand
	 * back the rung that worked plus the lag that rung measured, so the level
	 * find is also run one rather than a throwaway.
	 */
	async function _findLevel(bus: CueAlignBus, mic: MicHandle): Promise<{ gain: number; lagMs: number }> {
		const attempts: LevelAttempt[] = [];
		let best = 0;
		for (const gain of CUE_LATENCY_GAIN_STEPS) {
			effects.onProbe?.({ bus, gain, peak: null, lagMs: null, best, threshold: CUE_LATENCY_PEAK_MIN });
			const { lagMs, peak } = await _probe(bus, mic, gain);
			if (peak > best) best = peak;
			attempts.push({ gain, peak, lagMs });
			effects.onProbe?.({ bus, gain, peak, lagMs, best, threshold: CUE_LATENCY_PEAK_MIN });
			if (peak >= CUE_LATENCY_PEAK_TARGET) break;
		}
		const chosen = chooseLevelRung(attempts);
		if (chosen === null) throw new _MicCouldNotHear(bus, best);
		return chosen;
	}

	/**
	 * Stage two. One confirming chirp at the rung stage one settled on, and if
	 * that one capture falls short, ONE retry at full level before the run is
	 * refused: a bus heard clearly a second ago is far more likely to have been
	 * hit by a noise or a cup shifting than to have gone deaf. Full level, not
	 * the next rung: on the real modal the speakers read 0.52 in the ramp and
	 * 0.34 one rung louder moments later.
	 */
	async function _measure(bus: CueAlignBus, mic: MicHandle, gain: number): Promise<{ lagMs: number; gain: number }> {
		const first = await _probe(bus, mic, gain);
		effects.onProbe?.({ bus, gain, peak: first.peak, lagMs: first.lagMs, best: first.peak, threshold: CUE_LATENCY_PEAK_MIN });
		if (first.peak >= CUE_LATENCY_PEAK_MIN) return { lagMs: first.lagMs, gain };
		const louder = CUE_LATENCY_GAIN_STEPS[CUE_LATENCY_GAIN_STEPS.length - 1];
		effects.onProbe?.({ bus, gain: louder, peak: null, lagMs: null, best: first.peak, threshold: CUE_LATENCY_PEAK_MIN });
		const retry = await _probe(bus, mic, louder);
		effects.onProbe?.({
			bus,
			gain: louder,
			peak: retry.peak,
			lagMs: retry.lagMs,
			best: Math.max(first.peak, retry.peak),
			threshold: CUE_LATENCY_PEAK_MIN
		});
		if (retry.peak >= CUE_LATENCY_PEAK_MIN) return { lagMs: retry.lagMs, gain: louder };
		throw new _MicCouldNotHear(bus, Math.max(first.peak, retry.peak));
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

	/** One back-to-back master/cue pair through the verify injection points. Expected residual 0 ms. */
	async function _verifyResidual(mic: MicHandle, masterGain: number, cueGain: number): Promise<number> {
		const master = await _probe('master', mic, masterGain, 'verify');
		effects.onProbe?.({
			bus: 'master',
			gain: masterGain,
			peak: master.peak,
			lagMs: master.lagMs,
			best: master.peak,
			threshold: CUE_LATENCY_PEAK_MIN
		});
		const cue = await _probe('cue', mic, cueGain, 'verify');
		effects.onProbe?.({
			bus: 'cue',
			gain: cueGain,
			peak: cue.peak,
			lagMs: cue.lagMs,
			best: cue.peak,
			threshold: CUE_LATENCY_PEAK_MIN
		});
		// A lag read off a chirp the mic could not hear is noise, and scoring it
		// could pass a wrong plan or steer the retry by a random amount.
		for (const [bus, result] of [['speakers', master], ['headphones', cue]] as const) {
			if (result.peak < CUE_LATENCY_PEAK_MIN) {
				throw new Error(
					`verification could not hear the ${bus} (peak ${result.peak.toFixed(2)}, ` +
						`needs ${CUE_LATENCY_PEAK_MIN}); nothing was saved`
				);
			}
		}
		return cue.lagMs - master.lagMs;
	}

	async function _runBody(interactive: boolean, mic: MicHandle): Promise<void> {
		activePreRunDelays = effects.readDelays();
		_setStep('mic_check_master');
		const masterLevel = await _findLevel('master', mic);
		calibration.master_latency_ms = masterLevel.lagMs;
		_setStep('mic_check_cue');
		if (interactive) {
			await _waitForCueCheck();
			_throwIfAborted();
		}
		const cueLevel = await _findLevel('cue', mic);
		calibration.cue_latency_ms = cueLevel.lagMs;
		_setStep('measuring');
		// The result comes from back-to-back pairs only. The shared mic input drifts
		// a few ms per capture on BOTH buses (measured Wed 16 Sep 2026: speakers 151,
		// 159, 165 ms and headphones 190, 195, 201 ms), which cancels in cue minus
		// master when the two are measured seconds apart. The ramps are not paired:
		// an operator holding the ear cup in between can put many seconds of drift
		// between them.
		const master: number[] = [];
		const cue: number[] = [];
		const offsets: number[] = [];
		let verifyMasterGain = masterLevel.gain;
		let verifyCueGain = cueLevel.gain;
		for (let run = 0; run < CUE_ALIGN_RUNS; run += 1) {
			const masterResult = await _measure('master', mic, masterLevel.gain);
			master.push(masterResult.lagMs);
			verifyMasterGain = Math.max(verifyMasterGain, masterResult.gain);
			const cueResult = await _measure('cue', mic, cueLevel.gain);
			cue.push(cueResult.lagMs);
			verifyCueGain = Math.max(verifyCueGain, cueResult.gain);
			offsets.push(cue[run] - master[run]);
		}
		const spread = spreadMs(offsets);
		if (spread > CUE_ALIGN_MAX_SPREAD_MS) {
			throw new Error(
				`measurement unstable (spread ${spread} ms), try again with less room noise ` +
					`(speakers ${master.join(', ')} ms; headphones ${cue.join(', ')} ms)`
			);
		}
		const masterMs = median3(master);
		const cueMs = median3(cue);
		const offsetMs = median3(offsets);
		const plan = deriveAlignment(effects.alignmentMode(), offsetMs);
		const ids = effects.deviceIds();
		const record: AppliedAlignment['record'] = {
			cue_latency_ms: cueMs,
			master_latency_ms: masterMs,
			measured_at: new Date(effects.now()).toISOString(),
			cue_device_id: ids.cue,
			master_device_id: ids.master
		};
		// Applied provisionally: nothing reaches storage until verification passes,
		// so a cancelled or failed run cannot leave an unverified plan behind.
		effects.setDelays({ head_delay_ms: plan.head_delay_ms, master_delay_ms: plan.master_delay_ms });
		verifyDelaysApplied = true;
		calibration.master_latency_ms = masterMs;
		calibration.cue_latency_ms = cueMs;
		calibration.offset_ms = offsetMs;
		calibration.error = null;
		calibration.verify_residual_ms = null;
		_setStep('verifying');
		let workingOffsetMs = offsetMs;
		let finalPlan = plan;
		let residual = await _verifyResidual(mic, verifyMasterGain, verifyCueGain);
		const firstMiss = residual - intendedResidualMs(workingOffsetMs, plan);
		if (Math.abs(firstMiss) > CUE_ALIGN_VERIFY_TOLERANCE_MS) {
			workingOffsetMs = Math.round(workingOffsetMs + firstMiss);
			const retryPlan = deriveAlignment(effects.alignmentMode(), workingOffsetMs);
			effects.setDelays({
				head_delay_ms: retryPlan.head_delay_ms,
				master_delay_ms: retryPlan.master_delay_ms
			});
			const secondResidual = await _verifyResidual(mic, verifyMasterGain, verifyCueGain);
			const secondMiss = secondResidual - intendedResidualMs(workingOffsetMs, retryPlan);
			if (Math.abs(secondMiss) > CUE_ALIGN_VERIFY_TOLERANCE_MS) {
				throw new Error(
					`the fix did not hold after verification ` +
						`(attempt 1: ${firstMiss} ms off, attempt 2: ${secondMiss} ms off)`
				);
			}
			residual = secondResidual;
			finalPlan = retryPlan;
		}
		// The stored record carries the offset that verified, so a later mode change
		// re-derives the delays from the corrected figure rather than the first guess.
		effects.persist({
			head_delay_ms: finalPlan.head_delay_ms,
			master_delay_ms: finalPlan.master_delay_ms,
			record: { ...record, cue_latency_ms: record.master_latency_ms + workingOffsetMs }
		});
		verifyDelaysApplied = false;
		calibration.cue_latency_ms = record.master_latency_ms + workingOffsetMs;
		calibration.offset_ms = workingOffsetMs;
		calibration.verify_residual_ms = residual;
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
		calibration.verify_residual_ms = null;
		activePreRunDelays = null;
		verifyDelaysApplied = false;
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
			// Any failure after the provisional apply puts the previous delays back.
			if (verifyDelaysApplied && activePreRunDelays !== null) {
				effects.setDelays(activePreRunDelays);
			}
			if (error instanceof _Aborted || abortController.signal.aborted) {
				calibration.error = null;
				calibration.verify_residual_ms = null;
				_setStep('idle');
			} else {
				calibration.error = error instanceof Error ? error.message : String(error);
				_setStep('failed');
			}
		} finally {
			mic?.stop();
			if (paused.length > 0) await effects.resumeDecks(paused);
			activePreRunDelays = null;
			verifyDelaysApplied = false;
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
