/**
 * CUEOUT-14: the ONE calibration session the page owns.
 *
 * Composition only. The decision path is `createCueAlignController` in
 * player/cue-align.svelte.ts; the audio effects come from
 * `cueAlignAudioEffects()` in player/cue-align-audio.ts (bound to the live monitor
 * graph); the deck transport effects are the engine's own play/pause. Both the
 * CALIBRATE modal and the IPC commands `headphone_calibrate` /
 * `headphone_calibrate_abort` come through here, so GET /headphones mirrors
 * exactly the machine the operator is watching.
 *
 * `overrideCueAlignEffects` is the test seam: the unit harness replaces the
 * whole effects object (no mic, no chirp, no deck transport) and drives the
 * real IPC path. It is keyed on globalThis, like the mixerState singleton, so
 * a test bundle and the IPC bundle see the same override.
 */

import { DECK_IDS, type DeckId } from '$lib/player/constants';
// Types only: the controller itself is imported on demand in startCueAlignment,
// so the calibration state machine and its chirp maths stay out of the initial
// load of "/" (the library bundle budget). Nothing calibrates before a click.
import type { CueAlignController, CueAlignEffects, CueAlignProbe } from '$lib/player/cue-align.svelte';
import type { HeadphoneCalibrationProbe } from '$lib/rb/mixer-types';
import { deckStates, mixerState } from '$lib/player/state.svelte';
import { engine } from '$lib/rb/audio-engine.svelte';

export type CueAlignEffectsFactory = () => CueAlignEffects;

const OVERRIDE_KEY = '__mdtCueAlignEffectsOverride';

type _GlobalWithOverride = typeof globalThis & { [OVERRIDE_KEY]?: CueAlignEffectsFactory | null };

/** Test seam. `null` restores the real effects. */
export function overrideCueAlignEffects(factory: CueAlignEffectsFactory | null): void {
	if (factory !== null && typeof factory !== 'function') {
		throw new TypeError('overrideCueAlignEffects takes a factory function or null');
	}
	(globalThis as _GlobalWithOverride)[OVERRIDE_KEY] = factory;
}

function _deckTransportEffects(): Pick<CueAlignEffects, 'pauseDecks' | 'resumeDecks'> {
	return {
		async pauseDecks(): Promise<DeckId[]> {
			const playing = DECK_IDS.filter((deck) => deckStates[deck].playing);
			for (const deck of playing) await engine.pause(deck);
			return playing;
		},
		async resumeDecks(decks: readonly DeckId[]): Promise<void> {
			for (const deck of decks) await engine.play(deck);
		}
	};
}

async function _effects(): Promise<CueAlignEffects> {
	const override = (globalThis as _GlobalWithOverride)[OVERRIDE_KEY];
	if (override !== undefined && override !== null) return override();
	const { cueAlignAudioEffects } = await import('$lib/player/cue-align-audio');
	return { ...cueAlignAudioEffects(), ..._deckTransportEffects() };
}

/** Modal chrome state, read by Mixer.svelte. Closing while a run is in flight aborts it. */
export const cueAlignModal = $state({ open: false });

/**
 * Whether a run is in flight, as RUNE state.
 *
 * The controller also knows, but `_controller.running()` reads a plain closure
 * variable: nothing re-renders when it clears, so the button that reads it
 * stayed disabled for the rest of the page's life after the first failed run
 * (the last reactive write of a failing run is `step = 'failed'`, which happens
 * while the flag is still set). Keeping the flag here, in $state, is what makes
 * Run again clickable again.
 */
const _run = $state({ active: false });

/**
 * The controller's camelCase probe, in the serialized read model's snake_case.
 *
 * The probe lives in `mixerState.headphones.calibration`, not in a store beside
 * it, because that object is what `queryPerformanceState()` and
 * `GET /performance/headphones` serialize. A separate store showed the ear-cup
 * numbers to the modal alone, so a browser or HTTP agent driving the same
 * interactive step was working blind.
 */
function _probeReadModel(probe: CueAlignProbe): HeadphoneCalibrationProbe {
	return {
		bus: probe.bus,
		gain: probe.gain,
		peak: probe.peak,
		lag_ms: probe.lagMs,
		best: probe.best,
		threshold: probe.threshold
	};
}

let _controller: CueAlignController | null = null;
/** An abort that landed while the controller module was still loading. */
let _abortRequested = false;

export function openCueAlignModal(): void {
	cueAlignModal.open = true;
}

export function closeCueAlignModal(): void {
	abortCueAlignment();
	cueAlignModal.open = false;
}

export function cueAlignmentRunning(): boolean {
	return _run.active;
}

/**
 * Run one calibration against the live graph. Resolves on `applied`; rejects
 * with the calibration error on `failed` (so an HTTP caller reads a non-200
 * for a run that did not apply), and resolves on abort (step back to idle).
 * Refusing preconditions (no two_outputs graph) is recorded as `failed` too,
 * so the mirror never shows an `idle` machine for a request that was refused.
 */
export async function startCueAlignment(opts: { interactive: boolean }): Promise<void> {
	if (cueAlignmentRunning()) throw new Error('cue alignment calibration is already running');
	const calibration = mixerState.headphones.calibration;
	// Claimed before the first await, so a second start during the module load is
	// refused and an abort during it is honoured rather than lost.
	_run.active = true;
	_abortRequested = false;
	let effects: CueAlignEffects;
	let createCueAlignController: typeof import('$lib/player/cue-align.svelte').createCueAlignController;
	try {
		[effects, { createCueAlignController }] = await Promise.all([_effects(), import('$lib/player/cue-align.svelte')]);
	} catch (error) {
		_run.active = false;
		calibration.error = error instanceof Error ? error.message : String(error);
		calibration.step = 'failed';
		throw error;
	}
	if (_abortRequested) {
		_run.active = false;
		return;
	}
	const controller = createCueAlignController(
		{
			...effects,
			onProbe(probe) {
				calibration.probe = _probeReadModel(probe);
				effects.onProbe?.(probe);
			}
		},
		calibration
	);
	_controller = controller;
	calibration.probe = null;
	try {
		await controller.run(opts);
	} finally {
		if (_controller === controller) _controller = null;
		_run.active = false;
		calibration.probe = null;
	}
	if (calibration.step === 'failed') {
		throw new Error(calibration.error ?? 'cue alignment calibration failed');
	}
}

/** The operator has the ear cup on the mic (interactive runs only). */
export function continueCueAlignment(): void {
	if (_controller === null) throw new Error('no cue alignment calibration is running');
	_controller.continueCueCheck();
}

/** Safe to call when nothing is running: Escape, close, and the IPC abort all land here. */
export function abortCueAlignment(): void {
	if (_run.active && _controller === null) _abortRequested = true;
	_controller?.abort();
}
