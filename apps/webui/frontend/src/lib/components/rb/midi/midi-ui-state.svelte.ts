/**
 * Shared reactive UI state for the MIDI panel (build unit: midi panel).
 * Rune module - MUST stay .svelte.ts (rune_outside_svelte otherwise, same
 * bug class as webmidi.svelte.ts).
 *
 * Why a module and not component props: the TopBar label and the MidiPanel
 * drawer both need panelOpen + requestPending, and the amber-pulse state
 * ("prompt-pending") is only knowable by whoever kicked off initMidi().
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 toggleMidiPanel(): TopBar MIDI label click <-> drawer visibility.
 *     [if] two toggles don't return to the initial state [then] broken
 *   ✔︎ 🎯 requestMidiAccess(): wraps initMidi(); requestPending true only
 *     while the browser prompt is in flight; failure surfaces LOUDLY in
 *     midiUi.lastError + console.error (shown red in the panel - the UI is
 *     the error surface, nothing is swallowed silently).
 *     [if] initMidi() rejects and lastError stays null [then ⛔️] broken
 *     [if] a second call while pending doesn't throw [then ⛔️] broken
 */

import { initMidi } from '$lib/rb/midi/webmidi.svelte';
import { registerAllDeviceMaps } from '$lib/rb/midi/maps';
import { attachMidiGlue } from '$lib/rb/midi/action-glue.svelte';

// Device maps must be registered before initMidi resolves connected ports
// (else every device is "no map - learn log only"), and attachMidiGlue must
// run or every mapped action lands as "no action handler registered".
// Both were exported but never called anywhere - wire them here, once.
let _mapsRegistered = false;

export const midiUi: {
	panelOpen: boolean;
	requestPending: boolean;
	/** Last permission-request failure, shown red in the panel. null = none. */
	lastError: string | null;
} = $state({
	panelOpen: false,
	requestPending: false,
	lastError: null
});

export function toggleMidiPanel(): void {
	midiUi.panelOpen = !midiUi.panelOpen;
}

/** Request WebMIDI access via the core runtime. The catch is NOT silent
 * handling: the error lands in midiUi.lastError (rendered red in the panel)
 * and console.error - the panel IS the failure surface. */
export async function requestMidiAccess(): Promise<void> {
	if (midiUi.requestPending) {
		throw new Error('requestMidiAccess: a request is already pending');
	}
	midiUi.lastError = null;
	midiUi.requestPending = true;
	try {
		if (!_mapsRegistered) {
			registerAllDeviceMaps();
			attachMidiGlue();
			_mapsRegistered = true;
		}
		await initMidi();
	} catch (exc) {
		midiUi.lastError = exc instanceof Error ? exc.message : String(exc);
		console.error('[midi-panel] permission request failed', exc);
	} finally {
		midiUi.requestPending = false;
	}
}
