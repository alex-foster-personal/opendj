/**
 * The persisted "user enabled MIDI" choice, with no MIDI runtime attached.
 *
 * Why a leaf module: prefs-hydrate.ts applies the disk-backed opt-in during
 * prefs hydration. Importing it from midi-ui-state.svelte.ts dragged in
 * action-glue -> performance-ipc -> audio-engine, and audio-engine already
 * imports prefs, so the edge closed an import cycle. Under that cycle
 * performance-ipc's module body ran before audio-engine had bound
 * installScopedSyncRunner, and every bundle entering through performance-ipc
 * died with "installScopedSyncRunner is not a function" at load time.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 hydrateMidiEnabledFromDisk(): applies a boolean midi_enabled to
 *     localStorage and ignores a body that carries none.
 *     [if] a body without midi_enabled clears the stored choice [then ⛔️] broken
 *   ✔︎ 🎯 this module imports nothing from the MIDI or performance runtime.
 *     [if] performance-ipc.svelte.ts no longer loads in isolation [then ⛔️] broken
 */

import { bumpMidiEnabledTick, midiEnabledTick } from './midi-enabled-tick.svelte';

// localStorage is not reactive, so a UI reading the choice (the settings
// overlay's MIDI toggle) would never re-render on a toggle or when a denied
// request clears the choice later. Every write goes through
// persistMidiEnabled(), which bumps the tick; every read tracks it. The stored
// key stays the single source of truth; the tick carries no value of its own.

/** localStorage key for the "user enabled MIDI" choice. Set once the user
 * successfully grants access; read on page load to auto-re-request without a
 * second click. Namespaced so it never collides with other app keys. */
export const MIDI_ENABLED_KEY = 'dj:midi-enabled';

/** Persist (or clear) the user's MIDI-enabled choice. SSR/Node-safe: no-ops
 * where localStorage is absent, so importing this module server-side or in a
 * unit test never throws. */
export function persistMidiEnabled(enabled: boolean): void {
	if (typeof localStorage === 'undefined') return;
	if (enabled) {
		localStorage.setItem(MIDI_ENABLED_KEY, '1');
	} else {
		localStorage.removeItem(MIDI_ENABLED_KEY);
	}
	bumpMidiEnabledTick();
}

/** True if the user previously enabled MIDI (persisted choice). Lives in this
 * leaf so first-paint code (settings/apply.ts) can read the choice without
 * importing midi-ui-state and, through it, the WebMIDI runtime and device maps. */
export function midiEnabledPersisted(): boolean {
	void midiEnabledTick.n;
	if (typeof localStorage === 'undefined') return false;
	return localStorage.getItem(MIDI_ENABLED_KEY) === '1';
}

let _hydratedListener: ((enabled: boolean) => void) | null = null;

/** Register THE listener for a disk-hydrated choice. midi-ui-state registers
 * the auto-enable re-run here at import, because this leaf must never import
 * it (see the module docstring), and TopBar's one-shot `maybeAutoEnableMidi()`
 * at mount can run before the prefs GET has answered: without a re-run, a
 * `midi_enabled=true` that exists only on disk (another browser, an agent
 * PUT) never opted this browser in until a second reload (Codex P2, PR #3726). */
export function onMidiEnabledHydrated(listener: (enabled: boolean) => void): void {
	_hydratedListener = listener;
}

/** Apply disk-backed MIDI opt-in from GET /api/v1/ui-prefs (issue #2854). */
export function hydrateMidiEnabledFromDisk(body: { midi_enabled?: boolean }): void {
	if (typeof body.midi_enabled !== 'boolean') return;
	persistMidiEnabled(body.midi_enabled);
	_hydratedListener?.(body.midi_enabled);
}
