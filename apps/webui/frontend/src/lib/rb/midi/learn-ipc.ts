/**
 * MIDI learn IPC leaf: query snapshot + retry/cancel without coupling
 * performance-ipc (first paint) to the MIDI UI singleton.
 *
 * Why a leaf module: performance-ipc reports learn and runs agent commands,
 * and midi-ui-state owns the reactive learn machine on /performance. Neither
 * big module should import the other; this file registers an adapter from the
 * UI side once the MIDI runtime module loads.
 *
 * Requirements (mini-PRD):
 *   [if] queryPerformanceState before the adapter registers [then] midi_learn is idle, [else stop ⛔️]
 *   [if] this module imports runtime code from midi-ui-state [then] first paint pulls the MIDI UI graph, [else stop ⛔️]
 */

import type { InferredControlKind } from './learn-infer';
import type { MidiAction, MidiBinding } from './midi-types';

export type MidiLearnSnapshot = {
	phase: 'idle' | 'armed' | 'capturing' | 'ambiguous' | 'bound';
	target: { action: MidiAction; shift: boolean } | null;
	reason: string | null;
	kind: InferredControlKind | null;
	device_id: string | null;
	binding: MidiBinding | null;
};

export interface MidiLearnIpc {
	read(): MidiLearnSnapshot;
	retry(): Promise<void>;
	cancel(): void;
}

let _adapter: MidiLearnIpc | null = null;

export function registerMidiLearnIpc(adapter: MidiLearnIpc): () => void {
	_adapter = adapter;
	return () => {
		if (_adapter === adapter) _adapter = null;
	};
}

export const IDLE_MIDI_LEARN: MidiLearnSnapshot = {
	phase: 'idle',
	target: null,
	reason: null,
	kind: null,
	device_id: null,
	binding: null
};

export function readMidiLearn(): MidiLearnSnapshot {
	return _adapter?.read() ?? { ...IDLE_MIDI_LEARN };
}

export function midiLearnIpc(type: string): MidiLearnIpc {
	if (_adapter === null) {
		throw new Error(`${type} requires the MIDI runtime on /performance`);
	}
	return _adapter;
}
