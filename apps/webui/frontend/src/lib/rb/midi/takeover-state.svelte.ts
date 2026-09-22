/** IOPIN-06 runtime bridge: reactive ghosts + local Preview preference. */

import type { MidiAction } from './midi-types';
import {
	AbsoluteTakeoverPolicy,
	continuousTakeoverFunction,
	makeTakeoverIdentity,
	type MidiTakeoverMode,
	type TakeoverDecision,
	type TakeoverGhost
} from './takeover-policy';

const TAKEOVER_MODE_KEY = 'opendj.midi.takeover-mode';

function initialMode(): MidiTakeoverMode {
	if (typeof document === 'undefined') return 'pickup';
	return window.localStorage.getItem(TAKEOVER_MODE_KEY) === 'jump' ? 'jump' : 'pickup';
}

const policy = new AbsoluteTakeoverPolicy(initialMode());
const knownFunctions = new Set<string>();

/** Shared by MIDI settings and reusable scalar controls. */
export const midiTakeoverUi: {
	mode: MidiTakeoverMode;
	ghosts: Record<string, TakeoverGhost | null>;
} = $state({ mode: policy.mode, ghosts: {} });

function refreshGhost(functionId: string): void {
	knownFunctions.add(functionId);
	midiTakeoverUi.ghosts = { ...midiTakeoverUi.ghosts, [functionId]: policy.ghostForFunction(functionId) };
}

export function setMidiTakeoverMode(mode: MidiTakeoverMode): void {
	policy.setMode(mode);
	midiTakeoverUi.mode = mode;
	if (typeof document !== 'undefined') window.localStorage.setItem(TAKEOVER_MODE_KEY, mode);
	for (const functionId of knownFunctions) refreshGhost(functionId);
}

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

/** Reusable mixer chrome queries this by function id; no control invents a
 * ghost until MIDI has actually delivered an absolute value. */
export function midiTakeoverGhost(functionId: string): TakeoverGhost | null {
	return midiTakeoverUi.ghosts[functionId] ?? null;
}

/** The action-glue uses this to make the engine effect exhaustive without
 * duplicating action-union knowledge. */
export { continuousTakeoverFunction };
