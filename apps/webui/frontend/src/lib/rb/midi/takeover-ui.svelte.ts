/**
 * IOPIN-06 takeover display state, with no takeover policy attached.
 *
 * Why a leaf module: performance-ipc (first paint of "/") reports and sets the
 * takeover mode, and the mixer chrome (Knob, VFader, Crossfader, PitchFader,
 * TopBar's master) reads ghosts. Neither needs the pickup/jump POLICY, which
 * only runs once a controller delivers absolute values. Importing the policy
 * owner (takeover-state.svelte.ts) from those sites put the policy class in the
 * library bundle for every boot, controller or not. The policy owner imports
 * this module and registers for mode changes, so the reverse edge never exists.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 setMidiTakeoverMode(): updates the reactive mode, persists it, and
 *     forwards it to the policy owner once that module has loaded.
 *     [if] a mode set before the policy loads is not the policy's initial mode [then ⛔️] broken
 *     [if] a mode set after the policy loads does not reach policy.setMode [then ⛔️] broken
 *   ✔︎ this module imports nothing at runtime from the takeover policy.
 *     [if] takeover-policy.ts lands in the library bundle again [then ⛔️] broken
 */

import type { MidiTakeoverMode, TakeoverGhost } from './takeover-policy';

const TAKEOVER_MODE_KEY = 'opendj.midi.takeover-mode';

function initialMode(): MidiTakeoverMode {
	if (typeof document === 'undefined') return 'pickup';
	return window.localStorage.getItem(TAKEOVER_MODE_KEY) === 'jump' ? 'jump' : 'pickup';
}

/** Shared by MIDI settings and reusable scalar controls. */
export const midiTakeoverUi: {
	mode: MidiTakeoverMode;
	ghosts: Record<string, TakeoverGhost | null>;
} = $state({ mode: initialMode(), ghosts: {} });

let _modeListener: ((mode: MidiTakeoverMode) => void) | null = null;

/** Register THE policy owner's mode listener. takeover-state registers at
 * import and seeds its policy from midiTakeoverUi.mode, so a mode chosen
 * before the MIDI runtime loaded is still the one the policy starts in. */
export function onMidiTakeoverModeChange(listener: (mode: MidiTakeoverMode) => void): void {
	_modeListener = listener;
}

export function setMidiTakeoverMode(mode: MidiTakeoverMode): void {
	midiTakeoverUi.mode = mode;
	if (typeof document !== 'undefined') window.localStorage.setItem(TAKEOVER_MODE_KEY, mode);
	_modeListener?.(mode);
}

/** Reusable mixer chrome queries this by function id; no control invents a
 * ghost until MIDI has actually delivered an absolute value. */
export function midiTakeoverGhost(functionId: string): TakeoverGhost | null {
	return midiTakeoverUi.ghosts[functionId] ?? null;
}
