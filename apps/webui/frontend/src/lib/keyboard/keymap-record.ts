import {
	chordFromEvent,
	commandsForChord,
	effectiveKeymap,
	keymapConflicts,
	type Keymap,
	type KeymapOverrides
} from './keymap';
import { keymapCommandById, KEYMAP_COMMANDS } from './keymap-commands';

export type RecordKeymapOutcome =
	| { kind: 'cancelled' }
	| { kind: 'ignored' }
	| { kind: 'conflict'; label: string }
	| { kind: 'saved'; commandId: string; chord: string };

export function recordKeymapFromKeydown(
	preset: Keymap,
	overrides: KeymapOverrides,
	commandId: string,
	event: {
		code: string;
		key: string;
		metaKey: boolean;
		ctrlKey: boolean;
		altKey: boolean;
		shiftKey: boolean;
	},
	platform: string
): RecordKeymapOutcome {
	if (event.key === 'Escape') return { kind: 'cancelled' };
	const chord = chordFromEvent(event, platform);
	if (chord === null) return { kind: 'ignored' };

	const trial = effectiveKeymap(preset, { ...overrides, [commandId]: [chord] });
	const conflicts = keymapConflicts(trial);
	if (conflicts.length > 0) {
		const hit = conflicts.find((c) => c.commandIds.includes(commandId)) ?? conflicts[0];
		const otherId = hit.commandIds.find((id) => id !== commandId) ?? hit.commandIds[0];
		const label = keymapCommandById(otherId)?.label ?? otherId;
		return { kind: 'conflict', label };
	}
	const owners = commandsForChord(trial, chord, 'deck', KEYMAP_COMMANDS);
	if (owners.some((id) => id !== commandId)) {
		const otherId = owners.find((id) => id !== commandId)!;
		const label = keymapCommandById(otherId)?.label ?? otherId;
		return { kind: 'conflict', label };
	}
	return { kind: 'saved', commandId, chord };
}
