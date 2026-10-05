/** Controller pad modes for the hot-cue dropdown (CHROME-11). */

export interface PadModeEntry {
	id: string;
	label: string;
	built: boolean;
}

export const PAD_MODE_CATALOG: readonly PadModeEntry[] = [
	{ id: 'hot-cue', label: 'Hot Cue', built: true },
	{ id: 'beat-loop', label: 'Beat Loop / Roll', built: false },
	{ id: 'beat-jump', label: 'Beat Jump', built: false },
	{ id: 'sampler', label: 'Sampler', built: false },
	{ id: 'pad-fx-1', label: 'Pad FX 1', built: false },
	{ id: 'pad-fx-2', label: 'Pad FX 2', built: false },
	{ id: 'key-shift', label: 'Key Shift', built: false },
	{ id: 'keyboard', label: 'Keyboard', built: false }
];

export function padModeMenuLabel(entry: PadModeEntry): string {
	return entry.built ? entry.label : `${entry.label} (not-built-yet)`;
}
