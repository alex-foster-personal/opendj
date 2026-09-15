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

import { api } from '$lib/api/client';
import { attachMidiGlue } from '$lib/rb/midi/action-glue.svelte';
import { registerAllDeviceMaps } from '$lib/rb/midi/maps';
import { initMidi } from '$lib/rb/midi/webmidi.svelte';
import { makeDiskWriteChain } from '$lib/rb/disk-write-chain';

// Device maps must be registered before initMidi resolves connected ports
// (else every device is "no map - learn log only"), and attachMidiGlue must
// run or every mapped action lands as "no action handler registered".
// Both were exported but never called anywhere - wire them here, once.
let _mapsRegistered = false;

/** localStorage key for the "user enabled MIDI" choice. Set once the user
 * successfully grants access; read on page load to auto-re-request without a
 * second click. Namespaced so it never collides with other app keys. */
export const MIDI_ENABLED_KEY = 'dj:midi-enabled';

export const midiUi: {
	panelOpen: boolean;
	requestPending: boolean;
	/** Last permission-request failure, shown red in the panel. null = none. */
	lastError: string | null;
	/** Learn-log pop-out overlay: a click-through, minimizable floating log so
	 * mapping traffic stays visible while driving the app (no drawer needed). */
	logPopoutOpen: boolean;
	logPopoutMinimized: boolean;
} = $state({
	panelOpen: false,
	requestPending: false,
	lastError: null,
	logPopoutOpen: false,
	logPopoutMinimized: false
});

export function toggleMidiPanel(): void {
	midiUi.panelOpen = !midiUi.panelOpen;
}

// -------------------------------------------------------- learn-log pop-out

/** Open the floating learn-log overlay (always un-minimized on open). */
export function openLogPopout(): void {
	midiUi.logPopoutOpen = true;
	midiUi.logPopoutMinimized = false;
}

export function closeLogPopout(): void {
	midiUi.logPopoutOpen = false;
}

/** Collapse the overlay to its title bar (still on screen, out of the way). */
export function toggleLogPopoutMinimized(): void {
	midiUi.logPopoutMinimized = !midiUi.logPopoutMinimized;
}

// --------------------------------------------------- enabled-choice persistence

/** Persist (or clear) the user's MIDI-enabled choice. SSR/Node-safe: no-ops
 * where localStorage is absent, so importing this module server-side or in a
 * unit test never throws. */
const _syncMidiEnabledDisk = makeDiskWriteChain(async (patch: { midi_enabled: boolean }) => {
	try {
		await api.PUT('/api/v1/ui-prefs', { body: patch });
	} catch {
		/* localStorage remains authoritative if daemon is down */
	}
});

function _persistMidiEnabled(enabled: boolean): void {
	if (typeof localStorage === 'undefined') return;
	if (enabled) {
		localStorage.setItem(MIDI_ENABLED_KEY, '1');
	} else {
		localStorage.removeItem(MIDI_ENABLED_KEY);
	}
}

function _syncMidiEnabledToDisk(enabled: boolean): void {
	void _syncMidiEnabledDisk({ midi_enabled: enabled });
}

/** Agent parity: set the persisted opt-in without requesting WebMIDI access. */
export function setMidiEnabledChoice(enabled: boolean): void {
	_persistMidiEnabled(enabled);
	_syncMidiEnabledToDisk(enabled);
}

/** Apply disk-backed MIDI opt-in from GET /api/v1/ui-prefs (issue #2854). */
export function hydrateMidiEnabledFromDisk(body: { midi_enabled?: boolean }): void {
	if (typeof body.midi_enabled !== 'boolean') return;
	_persistMidiEnabled(body.midi_enabled);
}

/** True if the user previously enabled MIDI (persisted choice). */
export function midiEnabledPersisted(): boolean {
	if (typeof localStorage === 'undefined') return false;
	return localStorage.getItem(MIDI_ENABLED_KEY) === '1';
}

/** On page load, re-run the access request IFF the user opted in before. Goes
 * through requestMidiAccess() (the single init trigger that also registers
 * device maps + attaches the glue) so the invariant holds. No-op when the
 * choice was never made or a request is already in flight. */
export async function maybeAutoEnableMidi(): Promise<void> {
	if (!midiEnabledPersisted()) return;
	if (midiUi.requestPending) return;
	await requestMidiAccess();
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
		// Access granted: remember the choice so a reload auto-re-requests.
		_persistMidiEnabled(true);
		_syncMidiEnabledToDisk(true);
	} catch (exc) {
		midiUi.lastError = exc instanceof Error ? exc.message : String(exc);
		console.error('[midi-panel] permission request failed', exc);
		// Denied/unsupported: forget the choice so we don't nag on every reload
		// (the user re-opts-in from the panel when ready). Fail-fast, no retry.
		_persistMidiEnabled(false);
		_syncMidiEnabledToDisk(false);
	} finally {
		midiUi.requestPending = false;
	}
}
