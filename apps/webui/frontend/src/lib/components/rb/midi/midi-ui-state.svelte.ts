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
 *   ✔︎ detachMidiGlueForRouteUnmount(): stops the LED effect + channel-meter
 *     pump when the /performance route that owns the audio engine unmounts,
 *     so neither keeps polling a disposed engine on an unrelated route.
 *   ✔︎ requestMidiAccess() re-arms the glue on a later call (e.g. returning
 *     to /performance after navigating away, where maybeAutoEnableMidi()
 *     re-runs this): _mapsRegistered gates device-map registration (once
 *     ever) SEPARATELY from _detachMidiGlue (re-attached whenever null), so
 *     a return visit doesn't leave the LED effect + meter pump dead until a
 *     full reload.
 *     [if] requestMidiAccess() runs a second time after
 *       detachMidiGlueForRouteUnmount() [then ⛔️] glue must reattach, not
 *       stay null
 *   ✔︎ Installed-maps live refresh handles BOTH the targeted 'midi_maps' kind
 *     AND resync (events-bus.ts: "a consumer that handles a kind MUST also
 *     handle resync"), coalesced, so a PUT/DELETE the socket missed during a
 *     gap/reconnect/slow-consumer close still reaches the registry instead of
 *     staying stale until another edit or a page reload.
 *     [if] the socket reconnects after a missed midi_maps PUT and the
 *       registry is not reloaded [then ⛔️] broken
 */

import { initMidi } from '$lib/rb/midi/webmidi.svelte';
import { registerAllDeviceMaps } from '$lib/rb/midi/maps';
import { attachMidiGlue } from '$lib/rb/midi/action-glue.svelte';
import { loadInstalledDeviceMaps } from '$lib/rb/midi/installed-maps';
import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
import { coalesce } from '$lib/rb/coalesce';
import { api } from '$lib/api/client';
import { makeDiskWriteChain } from '$lib/rb/disk-write-chain';
import {
	midiEnabledPersisted,
	onMidiEnabledHydrated,
	persistMidiEnabled
} from './midi-enabled-choice';

export { midiEnabledPersisted };

// Device maps must be registered before initMidi resolves connected ports
// (else every device is "no map - learn log only"), and attachMidiGlue must
// run or every mapped action lands as "no action handler registered".
// Both were exported but never called anywhere - wire them here, once.
let _mapsRegistered = false;

// Armed once the first loadInstalledDeviceMaps() succeeds, so a later
// onboarding-wizard PUT/DELETE (routes/midi_maps.py publishes kind
// 'midi_maps') reloads the registry live instead of only at grant time.
let _installedMapsLiveRefreshArmed = false;

// Coalesced (see $lib/rb/coalesce docstring): a targeted 'midi_maps' event and
// a resync (reconnect/gap/slow-consumer) can fire close together for the same
// underlying change, and an unguarded pair would race two concurrent reloads
// writing the same registry.
const _reloadInstalledMaps = coalesce(async () => {
	try {
		await loadInstalledDeviceMaps();
		midiUi.installedMapsError = null;
	} catch (exc) {
		midiUi.installedMapsError = exc instanceof Error ? exc.message : String(exc);
		console.error('[midi-panel] installed device maps failed to reload', exc);
	}
});

function _reloadInstalledMapsAfterLibraryChange(): void {
	void _reloadInstalledMaps();
}

// The engine (and the LED effect + channel-meter pump that read it) only
// exists on the /performance route, but this module is a page-independent
// singleton - so the route owns tearing the glue down, not this module.
// Kept here (rather than a module-scoped teardown inside action-glue itself)
// because this is the one call site that attaches it.
let _detachMidiGlue: (() => void) | null = null;

export type MidiPanelWidthMode = 'compact' | 'expanded' | 'floating';

export const midiUi: {
	panelOpen: boolean;
	widthMode: MidiPanelWidthMode;
	requestPending: boolean;
	/** Last permission-request failure, shown red in the panel. null = none. */
	lastError: string | null;
	/** Learn-log pop-out overlay: a click-through, minimizable floating log so
	 * mapping traffic stays visible while driving the app (no drawer needed). */
	logPopoutOpen: boolean;
	logPopoutMinimized: boolean;
	/** Why the daemon's installed maps could not be loaded. Separate from
	 * lastError: the BUILTIN maps are still live when this is set, so it is a
	 * degraded state to show, not a failed permission request. */
	installedMapsError: string | null;
} = $state({
	panelOpen: false,
	widthMode: 'compact' as MidiPanelWidthMode,
	requestPending: false,
	lastError: null,
	logPopoutOpen: false,
	logPopoutMinimized: false,
	installedMapsError: null
});

export function toggleMidiPanel(): void {
	midiUi.panelOpen = !midiUi.panelOpen;
}

export function setMidiPanelWidthMode(mode: MidiPanelWidthMode): void {
	midiUi.widthMode = mode;
}

export function toggleMidiPanelExpanded(): void {
	midiUi.widthMode = midiUi.widthMode === 'expanded' ? 'compact' : 'expanded';
}

export function floatMidiPanel(): void {
	midiUi.widthMode = 'floating';
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

// The localStorage half lives in midi-enabled-choice.ts so prefs hydration can
// apply it without importing this module's MIDI runtime (see that file).
const _syncMidiEnabledDisk = makeDiskWriteChain(async (patch: { midi_enabled: boolean }) => {
	try {
		await api.PUT('/api/v1/ui-prefs', { body: patch });
	} catch {
		/* localStorage remains authoritative if daemon is down */
	}
});

function _syncMidiEnabledToDisk(enabled: boolean): void {
	void _syncMidiEnabledDisk({ midi_enabled: enabled });
}

/** Agent parity: set the persisted opt-in without requesting WebMIDI access. */
export function setMidiEnabledChoice(enabled: boolean): void {
	persistMidiEnabled(enabled);
	_syncMidiEnabledToDisk(enabled);
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

// The prefs GET lands after TopBar's mount-time maybeAutoEnableMidi() more
// often than not, so a choice that lives only on disk needs this second run.
// maybeAutoEnableMidi() is idempotent: it re-reads the persisted key and
// declines while a request is pending.
onMidiEnabledHydrated((enabled) => {
	if (enabled) void maybeAutoEnableMidi();
});

/** Request WebMIDI access via the core runtime. The catch is NOT silent
 * handling: the error lands in midiUi.lastError (rendered red in the panel)
 * and console.error - the panel IS the failure surface. */
export async function requestMidiAccess(): Promise<void> {
	if (midiUi.requestPending) {
		throw new Error('requestMidiAccess: a request is already pending');
	}
	midiUi.lastError = null;
	midiUi.requestPending = true;
	let attachedForThisRequest = false;
	try {
		if (!_mapsRegistered) {
			registerAllDeviceMaps();
			_mapsRegistered = true;
		}
		// Deliberately a SEPARATE condition from _mapsRegistered above: maps
		// register once per page lifetime, but the glue's LED effect + meter
		// pump stop on every /performance unmount (detachMidiGlueForRouteUnmount)
		// and must re-arm on the next grant/remount rather than staying dead
		// until a full reload. attachMidiGlue() itself is safe to call again -
		// see its own docstring.
		if (_detachMidiGlue === null) {
			_detachMidiGlue = attachMidiGlue();
			attachedForThisRequest = true;
		}
		await initMidi();
		// Access granted: remember the choice so a reload auto-re-requests.
		setMidiEnabledChoice(true);
		// Controllers onboarded in the app live in the daemon, not the bundle.
		// Loaded AFTER initMidi and in its own catch on purpose: a daemon that
		// cannot serve them must not cost the user the builtin maps mid-set.
		// This is degradation with a visible error in the panel, NOT a silent
		// fallback - installedMapsError is rendered, never swallowed.
		// Armed unconditionally, before the fetch below: a first load that fails
		// (daemon momentarily unreachable) must still leave these listeners live,
		// otherwise a resync/kind event arriving right after a failed initial
		// load would never re-arm the very listeners meant to recover it, and
		// the registry stays stale until a page reload.
		if (!_installedMapsLiveRefreshArmed) {
			_installedMapsLiveRefreshArmed = true;
			subscribeKind('midi_maps', _reloadInstalledMapsAfterLibraryChange);
			// A consumer that handles a kind MUST also handle resync
			// (events-bus.ts docstring): a gap, a slow-consumer close, or a
			// reconnect can each swallow a PUT/DELETE the socket was down for,
			// and without this the registry would stay stale until another
			// edit or a page reload - PR #511 review thread "Resync installed
			// maps after event-bus gaps".
			subscribeResync(_reloadInstalledMapsAfterLibraryChange);
		}
		try {
			await loadInstalledDeviceMaps();
			midiUi.installedMapsError = null;
		} catch (exc) {
			midiUi.installedMapsError = exc instanceof Error ? exc.message : String(exc);
			console.error('[midi-panel] installed device maps failed to load', exc);
		}
	} catch (exc) {
		// A denied or unsupported request never owns an active MIDI route. Tear
		// down the glue this attempt attached, including the 20 Hz meter timer,
		// so failure cannot leak work for the lifetime of the page/process. Do
		// not detach a pre-existing route if a later re-request failed.
		if (attachedForThisRequest && _detachMidiGlue !== null) {
			_detachMidiGlue();
			_detachMidiGlue = null;
		}
		midiUi.lastError = exc instanceof Error ? exc.message : String(exc);
		console.error('[midi-panel] permission request failed', exc);
		// Denied/unsupported: forget the choice so we don't nag on every reload
		// (the user re-opts-in from the panel when ready). Fail-fast, no retry.
		setMidiEnabledChoice(false);
	} finally {
		midiUi.requestPending = false;
	}
}

/** Stop the LED effect + channel-meter pump. Call from the /performance
 * route's own unmount, alongside engine disposal: the glue reads engine
 * state on a 30Hz timer, and without this call that timer outlives the
 * route, polling a disposed engine and sending stale meter CCs on whatever
 * page the user navigated to next. No-op if MIDI access was never granted
 * (glue never attached, nothing to stop). */
export function detachMidiGlueForRouteUnmount(): void {
	if (_detachMidiGlue === null) return;
	_detachMidiGlue();
	_detachMidiGlue = null;
}

/** TEST-ONLY: forget the one-time live-refresh arming (pair with
 * eventsBus._resetForTests, which drops the subscriptions this would skip
 * re-registering otherwise). */
export function _resetInstalledMapsLiveRefreshArmedForTests(): void {
	_installedMapsLiveRefreshArmed = false;
}
