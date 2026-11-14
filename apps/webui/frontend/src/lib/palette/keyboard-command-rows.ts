/**
 * CMDK-13797: every keymap and performance registry command is a Cmd K row (#13693, epic #9369).
 *
 * Pure: rows come from the keyboard agent handle's `list()` (`window.__odjKeyboard`, #13690), so
 * a row carries the command's label, its section, and the chord the active preset binds. Running
 * a row is `handle.run(id)`, the same call an agent makes. Ids a hand-written palette row or an
 * earlier bar row already offers are skipped, so no command shows twice.
 */
import type {
	KeyboardAgentHandle,
	KeyboardCommandListing
} from '$lib/keyboard/keyboard-agent-handle';
import { keymapCommandById } from '$lib/keyboard/keymap-commands';
import type { CommandAction } from '$lib/rb/cmdk-actions';

import { PALETTE_REGISTRY } from './commands';

type KeyboardCommandRunner = Pick<KeyboardAgentHandle, 'list' | 'run'>;

type KeyboardCommandRow = CommandAction & {
	section: string;
	command: { type: 'keyboard'; id: string };
};

/** Ids the hand-written palette already offers as runnable rows. */
const HAND_WRITTEN_IDS: ReadonlySet<string> = new Set(
	PALETTE_REGISTRY.commands.filter((entry) => entry.run !== null).map((entry) => entry.id)
);

//----- helpers

/** The keyboard handle the keymap runtime installed, or null where none is installed. */
export function installedKeyboardRunner(): KeyboardCommandRunner | null {
	return globalThis.window?.__odjKeyboard ?? null;
}

/** A chord as the bar spells hints: `Cmd+Shift+1` reads `Cmd Shift 1`, `Mod` reads `Cmd`. */
function keyboardChordHint(chord: string | undefined): string {
	if (chord === undefined || chord === '') return '';
	return chord
		.split('+')
		.map((part) => (part === 'Mod' ? 'Cmd' : part))
		.join(' ');
}

/** The section a command lists under: its keymap group, else its registry family. */
function keyboardCommandSection(listing: KeyboardCommandListing): string {
	if (listing.source === 'keymap') {
		const group = keymapCommandById(listing.id)?.group;
		if (group === undefined) throw new Error(`keymap command has no group: ${listing.id}`);
		return group;
	}
	if (listing.id.startsWith('preset.run.')) return 'Transition presets';
	if (listing.id.startsWith('selector.')) return 'Selector';
	return 'Dial and fader';
}

//----- rows

/** One bar row per listed keyboard command not already covered by a hand-written or `existing` row. */
export function keyboardCommandRows(
	runner: KeyboardCommandRunner | null,
	existing: readonly CommandAction[]
): KeyboardCommandRow[] {
	if (runner === null) return [];
	const taken = new Set([...HAND_WRITTEN_IDS, ...existing.map((row) => row.id)]);
	const out: KeyboardCommandRow[] = [];
	for (const listing of runner.list()) {
		if (taken.has(listing.id)) continue;
		taken.add(listing.id);
		const section = keyboardCommandSection(listing);
		out.push({
			kind: 'command',
			id: listing.id,
			label: listing.label,
			hint: keyboardChordHint(listing.chords[0]),
			keywords: [section, ...listing.id.split('.')],
			section,
			command: { type: 'keyboard', id: listing.id }
		});
	}
	return out;
}

/** Run a keyboard row through the shared by-id runner; null on success, else the bar's failure line. */
export function runKeyboardCommandRow(
	runner: KeyboardCommandRunner | null,
	row: Pick<CommandAction, 'label'> & { command: { type: 'keyboard'; id: string } }
): string | null {
	if (runner === null) return `${row.label} did not run (no_keyboard_runtime)`;
	try {
		const result = runner.run(row.command.id);
		return result.ok ? null : `${row.label} did not run (${result.refusal})`;
	} catch (exc) {
		return `${row.label} did not run: ${exc instanceof Error ? exc.message : String(exc)}`;
	}
}
