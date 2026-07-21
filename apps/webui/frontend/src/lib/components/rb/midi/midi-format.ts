/**
 * Pure formatting + status-derivation helpers for the MIDI panel UI
 * (build unit: midi panel). NO runes here so the module is unit-testable
 * without a component harness; all reactive state stays in
 * webmidi.svelte.ts / midi-ui-state.svelte.ts.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 hexByte/formatBytes: raw wire bytes -> uppercase hex for the
 *     learn-log console.
 *     [if] hexByte(0x90) isn't '90' [then] broken
 *     [if] hexByte(300) doesn't throw [then ⛔️] broken (fail-fast on
 *       non-byte input, never render garbage)
 *   ✔︎ 🎯 describeSource: decoded MidiSource -> human string; null (an
 *     undecoded status family) says so explicitly.
 *     [if] describeSource(null) is empty or '-' [then] broken (silent gap)
 *   ✔︎ 🎯 midiLabelStatus: single source of truth for the TopBar label
 *     colour. green = granted AND >=1 mapped device; amber = permission
 *     request in flight; grey = everything else.
 *     [if] granted with only UNMAPPED devices shows green [then ⛔️] broken
 */

import type { MidiSource } from '$lib/rb/midi/midi-types';
import type { MidiPermission } from '$lib/rb/midi/webmidi.svelte';

export type MidiLabelStatus = 'green' | 'amber' | 'grey';

// ---------------------------------------------------------------- _helpers

/** One wire byte -> two uppercase hex chars. Throws on non-byte input. */
export function hexByte(n: number): string {
	if (!Number.isInteger(n) || n < 0 || n > 255) {
		throw new RangeError(`hexByte: expected an integer 0..255, got ${n}`);
	}
	return n.toString(16).toUpperCase().padStart(2, '0');
}

/** Raw message triple -> '90 3C 7F' for the learn-log console. */
export function formatBytes(status: number, data1: number, data2: number): string {
	return `${hexByte(status)} ${hexByte(data1)} ${hexByte(data2)}`;
}

/** Decoded source -> 'ch 2 note 60' / 'ch 4 cc 31' / 'ch 1 pitchbend'.
 * null decode (status family outside P0 scope) is named explicitly -
 * the learn log must never show a silent gap. */
export function describeSource(src: MidiSource | null): string {
	if (src === null) return 'undecoded';
	if (src.kind === 'pitchbend') return `ch ${src.ch} pitchbend`;
	return `ch ${src.ch} ${src.kind} ${src.id}`;
}

/** performance.now() capture time -> '12.4s' since page load. */
export function formatLogTs(tsMs: number): string {
	if (!Number.isFinite(tsMs) || tsMs < 0) {
		throw new RangeError(`formatLogTs: expected a non-negative ms value, got ${tsMs}`);
	}
	return `${(tsMs / 1000).toFixed(1)}s`;
}

// ------------------------------------------------------- label status logic

/** TopBar MIDI label colour (unit spec):
 * grey  = unsupported / denied / idle-prompt / granted-without-mapped-device
 * amber = a permission request is currently in flight (prompt pending)
 * green = granted AND at least one connected device matched a DeviceMap */
export function midiLabelStatus(
	permission: MidiPermission,
	requestPending: boolean,
	hasMappedDevice: boolean
): MidiLabelStatus {
	if (requestPending) return 'amber';
	if (permission === 'granted' && hasMappedDevice) return 'green';
	return 'grey';
}

/** Tooltip for the TopBar MIDI label - states WHY the colour is what it is. */
export function midiLabelTitle(
	permission: MidiPermission,
	requestPending: boolean,
	mappedDeviceCount: number,
	deviceCount: number
): string {
	if (requestPending) return 'MIDI: permission request pending in the browser';
	if (permission === 'unsupported') return 'MIDI: WebMIDI not supported in this browser (use Chrome or Edge)';
	if (permission === 'denied') return 'MIDI: permission denied - re-enable in browser site settings';
	if (permission === 'prompt') return 'MIDI: click to open the panel and request access';
	if (permission === 'granted') {
		return `MIDI: ${mappedDeviceCount} mapped / ${deviceCount} connected device(s) - click for panel`;
	}
	const _exhaustive: never = permission;
	throw new Error(`Unhandled MidiPermission: ${_exhaustive}`);
}
