/**
 * The reactive WebMIDI surface state, with no MIDI runtime attached.
 *
 * Why a leaf module: the headphone cluster's MIDI status glyph is on the
 * /performance first paint and reads only this state. Importing it from
 * webmidi.svelte.ts made the whole WebMIDI/native transport (port scanning,
 * dispatch, LED queue, the Tauri event bridge) first-paint weight on every
 * boot, controller or not. webmidi writes this object and re-exports it, so
 * every existing import path keeps reading the one instance.
 *
 * Requirements (mini-PRD):
 *   ✔︎ this module imports nothing at runtime.
 *     [if] webmidi.svelte.ts lands in the performance bundle again [then ⛔️] broken
 */

export type MidiPermission = 'unsupported' | 'prompt' | 'granted' | 'denied';

/** One connected MIDI device as the UI + glue see it. */
export interface MidiDeviceInfo {
	/** WebMIDI input port id (primary identity for dispatch + LEDs). */
	id: string;
	name: string;
	manufacturer: string;
	/** vendor of the resolved DeviceMap; null = no map matched (learn-log
	 * only device). */
	mapVendor: string | null;
	/** True when a same-named output port exists (LED feedback possible). */
	hasOutput: boolean;
}

/** Reactive WebMIDI surface state. */
export const midiState: {
	permission: MidiPermission;
	devices: MidiDeviceInfo[];
	shiftHeld: boolean;
} = $state({
	permission: 'prompt',
	devices: [],
	shiftHeld: false
});
