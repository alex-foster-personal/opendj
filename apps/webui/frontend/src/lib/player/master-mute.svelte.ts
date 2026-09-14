/**
 * Opt-in startup master mute: a silence belt for headless multi-browser UI
 * test agents, on top of Chromium's `--mute-audio`.
 *
 * Activation is `?muted=1` on the /performance URL, read ONCE when the audio
 * graph is built. The param is strict: only the exact string '1' mutes, any
 * other value (including '0', 'true', 'yes', garbage) is ignored and the page
 * stays audible. That asymmetry is deliberate -- the headed client the maintainer uses is
 * the default, so an unrecognised value must never silence him, and a typo in a
 * test harness fails loudly as "still audible" rather than quietly as "muted".
 *
 * The mute is one GainNode sitting as the FINAL node before
 * AudioContext.destination, and muting sets ONLY that node's gain to 0. Nothing
 * upstream changes: decks, EQ, crossfader, analysers, waveform taps and the
 * performance IPC all run byte-for-byte as they would unmuted. The point is
 * that audio-path bugs still surface in a silent browser, so this module never
 * disconnects, bypasses or early-returns any part of the graph.
 *
 * Programmatic parity (house rule: every UI control has a non-UI path):
 * - `isMasterMuted()` / `setMasterMuted(bool)` for in-app callers.
 * - `window.__mdtMasterMute` for a test agent driving the page from the
 *   outside, where the ES module scope is unreachable (same shape as the
 *   existing `__mdtPerfLog` / `__jobProgressSimulate` bridges).
 *
 * The user's own mute survives a reload (UXR-01, adversarial UX round 1, Mon 14
 * Sep 2026): every setMasterMuted call stores the choice in localStorage under
 * MASTER_MUTE_STORAGE_KEY, and startup mutes when EITHER the URL says `muted=1`
 * OR the stored value is exactly '1'. Before this, muting and then reloading
 * brought the audio back unannounced, which is the unsafe direction for a mute.
 * The stored value only ever comes from an explicit mute in this browser
 * profile, so it cannot silence a headed client by accident the way a stray
 * query value could, and a stored unmute can never override `?muted=1`.
 *
 * Regression lines:
 * - if a mute made before a reload is lost after it then the page comes back
 *   audible without the user asking
 * - if `?muted=1` does not drive the master mute gain to exactly 0 then the
 *   headless silence belt is off and a fan-out of agents plays audio out loud
 * - if unmuting does not restore exactly 1 then the mute is lossy and the
 *   headed client comes back quieter than it started
 * - if muting disconnects or bypasses any node then a silent browser stops
 *   exercising the audio path and audio bugs hide until a headed run
 * - if a value other than '1' mutes then a stray query param silences the maintainer
 */

/** The query parameter that arms the startup mute. Exact match on '1'. */
export const MASTER_MUTE_PARAM = 'muted';

/** Master mute gain endpoints. Exact 0 and 1 -- never a ramp, never an epsilon. */
export const MASTER_MUTE_GAIN = 0;
export const MASTER_UNMUTE_GAIN = 1;

//-----------------------------------------------------------------------------
// query parameter
//-----------------------------------------------------------------------------

/** Strictly `muted=1`. Anything else -- absent, '0', 'true', '', garbage,
 * repeated -- leaves the page audible. */
export function parseMasterMutedParam(search: string): boolean {
	return new URLSearchParams(search).get(MASTER_MUTE_PARAM) === '1';
}

/** The startup value for this document: the URL belt OR the user's stored
 * mute. False under SSR, where there is no location and no audio graph. */
export function startupMasterMuted(): boolean {
	if (typeof window === 'undefined') return false;
	const search = window.location?.search;
	const fromUrl = typeof search === 'string' && parseMasterMutedParam(search);
	return fromUrl || readStoredMasterMuted(_browserStorage());
}

//-----------------------------------------------------------------------------
// stored choice
//-----------------------------------------------------------------------------

/** localStorage key for the user's last mute: exactly '1', or absent. */
export const MASTER_MUTE_STORAGE_KEY = 'odj.master-muted.v1';

type MuteStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

/** Strictly '1', like the query param. Storage that throws (private mode, a
 * locked-down webview) reads as not stored and says so in the console. */
export function readStoredMasterMuted(storage: MuteStorage | null): boolean {
	if (storage === null) return false;
	try {
		return storage.getItem(MASTER_MUTE_STORAGE_KEY) === '1';
	} catch (exc) {
		console.warn(`master mute: stored choice unreadable, starting audible: ${String(exc)}`);
		return false;
	}
}

/** Store '1' for muted and remove the key for unmuted, so nothing but an
 * explicit mute can ever read back as one. */
export function writeStoredMasterMuted(storage: MuteStorage | null, muted: boolean): void {
	if (storage === null) return;
	try {
		if (muted) storage.setItem(MASTER_MUTE_STORAGE_KEY, '1');
		else storage.removeItem(MASTER_MUTE_STORAGE_KEY);
	} catch (exc) {
		console.warn(`master mute: could not store the choice, it will not survive a reload: ${String(exc)}`);
	}
}

function _browserStorage(): MuteStorage | null {
	if (typeof window === 'undefined') return null;
	try {
		return window.localStorage ?? null;
	} catch (exc) {
		console.warn(`master mute: localStorage unavailable: ${String(exc)}`);
		return null;
	}
}

//-----------------------------------------------------------------------------
// state
//-----------------------------------------------------------------------------

/** Reactive so the topbar toggle repaints when a test agent flips the mute
 * through the global bridge rather than through the button.
 *
 * The URL is read HERE, once, at module load -- not inside the engine's graph
 * build. That ordering is what lets a caller who muted before the first
 * AudioContext exists keep their choice: graph creation only adopts the node
 * and stamps the standing value onto it, it never re-reads the URL. */
let _muted = $state(startupMasterMuted());

/** The final pre-destination GainNode, once the engine has built the graph.
 * Null before init and after dispose; the mute state survives both. */
let _gainNode: GainNode | null = null;

/** Gain the master mute node must carry for a given mute state. */
export function masterMuteGainValue(muted: boolean): number {
	return muted ? MASTER_MUTE_GAIN : MASTER_UNMUTE_GAIN;
}

export function isMasterMuted(): boolean {
	return _muted;
}

/** Set the mute and push it to the gain node. Value-only: the node keeps every
 * connection it has, so the graph upstream and downstream is untouched. */
export function setMasterMuted(muted: boolean): void {
	if (typeof muted !== 'boolean') throw new TypeError('setMasterMuted: muted must be boolean');
	_muted = muted;
	_applyMasterMute();
	writeStoredMasterMuted(_browserStorage(), muted);
}

function _applyMasterMute(): void {
	if (_gainNode === null) return;
	_gainNode.gain.value = masterMuteGainValue(_muted);
}

//-----------------------------------------------------------------------------
// engine wiring
//-----------------------------------------------------------------------------

/** Adopt the node the engine built as the last hop before the destination, and
 * stamp the current mute onto it. Called with null on dispose. */
export function attachMasterMuteNode(node: GainNode | null): void {
	_gainNode = node;
	_applyMasterMute();
}

/** The node the mute currently drives -- exported for the engine's teardown and
 * for tests asserting the graph is still wired while muted. */
export function masterMuteNode(): GainNode | null {
	return _gainNode;
}

//-----------------------------------------------------------------------------
// out-of-page bridge
//-----------------------------------------------------------------------------

/** `window.__mdtMasterMute` for headless agents: `.get()` / `.set(bool)`.
 * Installed once at module load; browser only. */
export function installMasterMuteGlobal(): void {
	if (typeof window === 'undefined') return;
	const w = window as Window & {
		__mdtMasterMute?: { get: () => boolean; set: (muted: boolean) => void };
	};
	w.__mdtMasterMute = { get: () => isMasterMuted(), set: (muted) => setMasterMuted(muted) };
}

installMasterMuteGlobal();
