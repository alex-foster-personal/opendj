/**
 * Keymap chord spelling and preset resolution (issue #10351 / KEYB-11488).
 *
 * #10347 adds the same file. Whichever merges second must union both: keep #10347's
 * default keymap and overlay derivation, keep this file's preset lookup.
 */

import { assistantChordUsesMeta } from '$lib/components/rb/hotkeys/hotkeys-registry';

import type { CommandScope } from './command-registry';
import { KEYMAP_COMMANDS } from './keymap-commands';
import { keymapPreset, type KeymapPresetId } from './presets/index';

export type Chord = string;

export interface Keymap {
	id: string;
	label: string;
	bindings: Readonly<Record<string, readonly Chord[]>>;
}

export type KeymapOverrides = Readonly<Record<string, readonly Chord[]>>;

export interface KeymapConflict {
	chord: Chord;
	scope: CommandScope;
	commandIds: string[];
}

const MODIFIER_CODES = new Set([
	'ShiftLeft',
	'ShiftRight',
	'ControlLeft',
	'ControlRight',
	'AltLeft',
	'AltRight',
	'MetaLeft',
	'MetaRight'
]);

const CODE_TO_KEY: Record<string, string> = {
	Minus: '-',
	Equal: '=',
	BracketLeft: '[',
	BracketRight: ']',
	Semicolon: ';',
	Quote: "'",
	Comma: ',',
	Period: '.',
	Slash: '/',
	Backslash: '\\',
	Backquote: 'Backquote'
};

const NAMED_CODES = new Set([
	'Space',
	'Enter',
	'Escape',
	'Tab',
	'Backspace',
	'ArrowUp',
	'ArrowDown',
	'ArrowLeft',
	'ArrowRight'
]);

function codeToKeyPart(code: string): string | null {
	if (MODIFIER_CODES.has(code)) return null;
	if (code.startsWith('Key')) return code.slice(3);
	if (code.startsWith('Digit')) return code.slice(5);
	if (code in CODE_TO_KEY) return CODE_TO_KEY[code];
	if (/^F\d{1,2}$/.test(code)) return code;
	if (NAMED_CODES.has(code)) return code;
	return null;
}

export function chordFromEvent(
	e: {
		code: string;
		metaKey: boolean;
		ctrlKey: boolean;
		altKey: boolean;
		shiftKey: boolean;
	},
	platform = globalThis.navigator?.platform ?? ''
): Chord | null {
	const keyPart = codeToKeyPart(e.code);
	if (keyPart === null) return null;

	const usesMeta = assistantChordUsesMeta(platform);
	const parts: string[] = [];
	if (usesMeta ? e.metaKey : e.ctrlKey) parts.push('Cmd');
	if (usesMeta ? e.ctrlKey : e.metaKey) parts.push(usesMeta ? 'Ctrl' : 'Meta');
	if (e.altKey) parts.push('Alt');
	if (e.shiftKey) parts.push('Shift');
	parts.push(keyPart);
	return parts.join('+');
}

const GLOBAL_DECK_FAMILY: ReadonlySet<CommandScope> = new Set(['global', 'deck']);

function scopeFamily(scope: CommandScope): CommandScope {
	return GLOBAL_DECK_FAMILY.has(scope) ? 'global' : scope;
}

function scopesMatch(a: CommandScope, b: CommandScope): boolean {
	if (a === b) return true;
	return GLOBAL_DECK_FAMILY.has(a) && GLOBAL_DECK_FAMILY.has(b);
}

export function commandsForChord(
	keymap: Keymap,
	chord: Chord,
	scope: CommandScope,
	commands = KEYMAP_COMMANDS
): string[] {
	const matches: string[] = [];
	for (const cmd of commands) {
		if (!scopesMatch(cmd.scope, scope)) continue;
		const chords = keymap.bindings[cmd.id];
		if (chords?.includes(chord)) matches.push(cmd.id);
	}
	return matches;
}

export function keymapConflicts(keymap: Keymap, commands = KEYMAP_COMMANDS): KeymapConflict[] {
	const byChordScope = new Map<string, string[]>();
	for (const cmd of commands) {
		const chords = keymap.bindings[cmd.id];
		if (!chords?.length) continue;
		const family = scopeFamily(cmd.scope);
		for (const chord of chords) {
			const k = `${chord}\0${family}`;
			const list = byChordScope.get(k) ?? [];
			list.push(cmd.id);
			byChordScope.set(k, list);
		}
	}
	const conflicts: KeymapConflict[] = [];
	for (const [key, commandIds] of byChordScope) {
		if (commandIds.length <= 1) continue;
		const sep = key.indexOf('\0');
		const chord = key.slice(0, sep);
		const scope = key.slice(sep + 1) as CommandScope;
		conflicts.push({ chord, scope, commandIds });
	}
	return conflicts;
}

export function effectiveKeymap(
	preset: Keymap,
	overrides: KeymapOverrides,
	commands = KEYMAP_COMMANDS
): Keymap {
	const validIds = new Set(commands.map((c) => c.id));
	const bindings: Record<string, readonly Chord[]> = { ...preset.bindings };
	const warned = new Set<string>();

	for (const [id, chords] of Object.entries(overrides)) {
		if (!validIds.has(id)) {
			if (!warned.has(id)) {
				console.warn(`keymap override ignored unknown command: ${id}`);
				warned.add(id);
			}
			continue;
		}
		bindings[id] = chords;
	}

	return { id: preset.id, label: preset.label, bindings };
}

export function getKeymapPreset(id: string): Keymap {
	return keymapPreset(id);
}

const NARROW_SCOPES: readonly CommandScope[] = ['dial', 'library', 'fader'];

export function resolveKeymapKeydown(
	keymap: Keymap,
	event: {
		code: string;
		metaKey: boolean;
		ctrlKey: boolean;
		altKey: boolean;
		shiftKey: boolean;
	},
	ctx: { platform?: string; activeScopes: CommandScope[] }
): string | null {
	const chord = chordFromEvent(event, ctx.platform);
	if (chord === null) return null;

	for (const scope of NARROW_SCOPES) {
		if (!ctx.activeScopes.includes(scope)) continue;
		const ids = commandsForChord(keymap, chord, scope);
		if (ids.length > 0) return ids[0];
	}
	for (const scope of ['deck', 'global'] as const) {
		const ids = commandsForChord(keymap, chord, scope);
		if (ids.length > 0) return ids[0];
	}
	return null;
}

export function libraryMovementYields(keymap: Keymap, key: string): boolean {
	const chord = key.length === 1 ? key.toUpperCase() : key;
	if (!['W', 'A', 'S', 'D'].includes(chord)) return false;
	return commandsForChord(keymap, chord, 'deck').length > 0;
}

export function legacyPerformanceChordsActive(presetId: string): boolean {
	return presetId === 'opendj';
}

/** True when the keymap runtime should handle this chord instead of legacy routers. */
export function keymapOwnsKeydown(
	state: { preset: KeymapPresetId; overrides: KeymapOverrides },
	chord: Chord | null
): boolean {
	if (chord === null) return false;
	if (state.preset === 'opendj' && Object.keys(state.overrides).length === 0) return false;
	if (state.preset === 'opendj') {
		for (const chords of Object.values(state.overrides)) {
			if (chords?.includes(chord)) return true;
		}
		return false;
	}
	return true;
}
