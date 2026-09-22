/**
 * CUEOUT-14: the ONE calibration session the page owns.
 *
 * Composition only. The decision path is `createCueAlignController` in
 * player/cue-align.svelte.ts; the audio effects come from
 * `cueAlignAudioEffects()` in player/headphones.ts (bound to the live monitor
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
import { createCueAlignController, type CueAlignController, type CueAlignEffects } from '$lib/player/cue-align.svelte';
import { cueAlignAudioEffects, recordHeadphoneFailureDiagnostic } from '$lib/player/headphones';
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

function _effects(): CueAlignEffects {
	const override = (globalThis as _GlobalWithOverride)[OVERRIDE_KEY];
	if (override !== undefined && override !== null) return override();
	return { ...cueAlignAudioEffects(), ..._deckTransportEffects() };
}

/** Modal chrome state, read by Mixer.svelte. Closing while a run is in flight aborts it. */
export const cueAlignModal = $state({ open: false });

let _controller: CueAlignController | null = null;

export function openCueAlignModal(): void {
	cueAlignModal.open = true;
}

export function closeCueAlignModal(): void {
	abortCueAlignment();
	cueAlignModal.open = false;
}

export function cueAlignmentRunning(): boolean {
	return _controller !== null && _controller.running();
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
	let effects: CueAlignEffects;
	try {
		effects = _effects();
	} catch (error) {
		calibration.error = error instanceof Error ? error.message : String(error);
		calibration.step = 'failed';
		recordHeadphoneFailureDiagnostic('calibration-precondition', error);
		throw error;
	}
	const controller = createCueAlignController(effects, calibration);
	_controller = controller;
	try {
		await controller.run(opts);
	} finally {
		if (_controller === controller) _controller = null;
	}
	if (calibration.step === 'failed') {
		const error = new Error(calibration.error ?? 'cue alignment calibration failed');
		recordHeadphoneFailureDiagnostic('calibration', error);
		throw error;
	}
}

/** The operator has the ear cup on the mic (interactive runs only). */
export function continueCueAlignment(): void {
	if (_controller === null) throw new Error('no cue alignment calibration is running');
	_controller.continueCueCheck();
}

/** Safe to call when nothing is running: Escape, close, and the IPC abort all land here. */
export function abortCueAlignment(): void {
	_controller?.abort();
}
