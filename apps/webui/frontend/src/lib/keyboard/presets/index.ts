import type { Keymap } from '../keymap';
import { KEYMAP_COMMANDS, type KeymapCommand } from '../keymap-commands';
import { OPENDJ_KEYMAP } from './opendj';
import { REKORDBOX_KEYMAP } from './rekordbox';
import { SERATO_KEYMAP } from './serato';

export const KEYMAP_PRESET_IDS = ['opendj', 'rekordbox', 'serato'] as const;
export type KeymapPresetId = (typeof KEYMAP_PRESET_IDS)[number];

export const KEYMAP_PRESETS: Record<KeymapPresetId, Keymap> = {
	opendj: OPENDJ_KEYMAP,
	rekordbox: REKORDBOX_KEYMAP,
	serato: SERATO_KEYMAP
};

export function isKeymapPresetId(value: unknown): value is KeymapPresetId {
	return KEYMAP_PRESET_IDS.find((id) => id === value) !== undefined;
}

export function keymapPreset(id: string): Keymap {
	if (!isKeymapPresetId(id)) {
		throw new Error(`unknown keymap preset: ${String(id)}`);
	}
	return KEYMAP_PRESETS[id];
}

export function unboundCommands(preset: KeymapPresetId | Keymap): KeymapCommand[] {
	const keymap = typeof preset === 'string' ? KEYMAP_PRESETS[preset] : preset;
	return KEYMAP_COMMANDS.filter((cmd) => {
		const chords = keymap.bindings[cmd.id];
		return !chords?.length;
	});
}
