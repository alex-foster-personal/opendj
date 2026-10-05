/** IOPIN-06 runtime bridge: the takeover policy, feeding the reactive ghosts
 * and the Preview preference that live in the takeover-ui.svelte.ts leaf. */

import { untrack } from 'svelte';
import type { MidiAction } from './midi-types';
import {
	AbsoluteTakeoverPolicy,
	continuousTakeoverFunction,
	makeTakeoverIdentity,
	type TakeoverDecision
} from './takeover-policy';
import { midiTakeoverUi, onMidiTakeoverModeChange } from './takeover-ui.svelte';
import { registerTakeoverRearm } from './webmidi.svelte';

// Seeded from the leaf, which may have been set (IPC, MIDI settings) before
// this module loaded with the rest of the MIDI runtime.
const policy = new AbsoluteTakeoverPolicy(midiTakeoverUi.mode);
const knownFunctions = new Set<string>();

function refreshGhost(functionId: string): void {
	knownFunctions.add(functionId);
	const next = policy.ghostForFunction(functionId);
	// This function is called from the engine-owned takeover $effect. Reading
	// then replacing the reactive ghost object there made that effect subscribe
	// to its own write, spinning until Svelte's update-depth guard intervened.
	// Ghost state is a display projection, never an input to policy decisions.
	const current = untrack(() => midiTakeoverUi.ghosts[functionId] ?? null);
	if (
		current === next ||
		(current !== null && next !== null && current.value === next.value && current.target === next.target)
	) {
		return;
	}
	const ghosts = untrack(() => midiTakeoverUi.ghosts);
	midiTakeoverUi.ghosts = { ...ghosts, [functionId]: next };
}

onMidiTakeoverModeChange((mode) => {
	policy.setMode(mode);
	for (const functionId of knownFunctions) refreshGhost(functionId);
});

/** Apply the policy at the real action-glue boundary. Undefined identity is
 * intentionally immediate: direct programmatic glue calls have no physical
 * controller position to pick up and must not masquerade as hardware. */
export function observeAbsoluteMidi(
	action: MidiAction,
	value: number,
	step: number,
	deviceId: string | undefined,
	controlId: string | undefined,
	softwareValue: number
): TakeoverDecision | null {
	if (deviceId === undefined || controlId === undefined) return null;
	const identity = makeTakeoverIdentity(deviceId, controlId, action);
	if (identity === null) return null;
	const decision = policy.observeAbsolute({ identity, hardwareValue: value, softwareValue, step });
	refreshGhost(identity.functionId);
	return decision;
}

/** Mark an accepted hardware value before dispatch updates the engine read model. */
export function noteAbsoluteMidiApplied(
	action: MidiAction,
	value: number,
	deviceId: string | undefined,
	controlId: string | undefined
): void {
	if (deviceId === undefined || controlId === undefined) return;
	const identity = makeTakeoverIdentity(deviceId, controlId, action);
	if (identity === null) return;
	policy.noteHardwareApplied(identity, value);
	refreshGhost(identity.functionId);
}

/** Called from the engine-owned glue effect for UI, IPC, preset, and hardware
 * edits alike. Only controls observed on a device are rearmed by the policy. */
export function noteTakeoverSoftwareValue(functionId: string, value: number): void {
	policy.noteSoftwareValue(functionId, value);
	refreshGhost(functionId);
}

export function rearmMidiTakeoverDevice(deviceId: string): void {
	policy.rearmDevice(deviceId);
	for (const functionId of knownFunctions) refreshGhost(functionId);
}

// webmidi calls this on connect, disconnect and a shift-layer change.
registerTakeoverRearm(rearmMidiTakeoverDevice);

/** The action-glue uses this to make the engine effect exhaustive without
 * duplicating action-union knowledge. */
export { continuousTakeoverFunction };
