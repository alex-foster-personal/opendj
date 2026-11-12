/** Controller pad modes for the hot-cue dropdown (CHROME-11). */

export interface PadModeEntry {
	id: string;
	label: string;
	built: boolean;
}

export interface PadModeOptionSnapshot {
	id: string;
	label: string;
	built: boolean;
	enabled: boolean;
	reason: string | null;
}

export interface PadModeSnapshot {
	selected: string;
	modes: PadModeOptionSnapshot[];
}

export const PAD_MODE_CATALOG: readonly PadModeEntry[] = [
	{ id: 'hot-cue', label: 'Hot Cue', built: true },
	{ id: 'beat-loop', label: 'Beat Loop / Roll', built: false },
	{ id: 'beat-jump', label: 'Beat Jump', built: true },
	{ id: 'sampler', label: 'Sampler', built: false },
	{ id: 'pad-fx-1', label: 'Pad FX 1', built: true },
	{ id: 'pad-fx-2', label: 'Pad FX 2', built: true },
	{ id: 'key-shift', label: 'Key Shift', built: false },
	{ id: 'keyboard', label: 'Keyboard', built: false }
];

export const PAD_MODE_IDS: readonly string[] = PAD_MODE_CATALOG.map((entry) => entry.id);

export function isPadModeId(value: unknown): value is string {
	return typeof value === 'string' && (PAD_MODE_IDS as readonly string[]).includes(value);
}

export function padModeMenuLabel(entry: PadModeEntry): string {
	return entry.built ? entry.label : `${entry.label} (not-built-yet)`;
}
