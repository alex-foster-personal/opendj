/**
 * Keyboard coverage report (#13689, epic #10346): how far the app is from full keyboard control.
 *
 * Pure data, no DOM. The command universe is every `KEYMAP_COMMANDS` id plus every
 * `PERFORMANCE_COMMAND_REGISTRY` id. For each command it asks one question: does the preset
 * bind a chord to it (registry commands also count their own `defaultKeys`)? There is no Cmd-K
 * bucket: every one of these commands is a Cmd-K row through `palette/keyboard-command-rows.ts`
 * (#13797), so a "no palette row" count is 0 by construction and could never move (#14712).
 * The /performance letter and digit shortcuts are counted separately by probing the pure
 * router with synthetic key events.
 */
import { resolvePerformanceShortcutAction } from '$lib/rb/performance-shortcut-routing';
import { PERFORMANCE_COMMAND_REGISTRY, type CommandScope } from './command-registry';
import type { Keymap } from './keymap';
import { KEYMAP_COMMANDS } from './keymap-commands';
import { KEYMAP_PRESETS, KEYMAP_PRESET_IDS, type KeymapPresetId } from './presets/index';

export interface CoverageCommand {
	id: string;
	source: 'keymap' | 'registry';
	scopes: readonly CommandScope[];
	defaultKeys: readonly string[];
}

export interface CoverageSources {
	commands: readonly CoverageCommand[];
	presets: Readonly<Record<KeymapPresetId, Keymap>>;
	performanceShortcuts: readonly PerformanceShortcutChord[];
}

export interface PerformanceShortcutChord {
	chord: string;
	action: string;
}

export interface KeyboardCoverageCounts {
	keymapCommands: number;
	registryCommands: number;
	noChord: Record<KeymapPresetId, number>;
	performanceLetterDigitChords: number;
}

export interface KeyboardCoverageReport {
	counts: KeyboardCoverageCounts;
	noChord: Record<KeymapPresetId, string[]>;
	performanceShortcuts: PerformanceShortcutChord[];
}

export interface CoverageBreach {
	bucket: string;
	count: number;
	ceiling: number;
	ids: string[];
}

export type CoverageCeilings = Omit<KeyboardCoverageCounts, 'keymapCommands' | 'registryCommands'>;

//------------------------------------------------------------------ /performance probe

const SHIFTED_DIGITS = ')!@#$%^&*(';
const PROBE_KEYS = [...'abcdefghijklmnopqrstuvwxyz', ...'0123456789'];
const PROBE_MODIFIERS = [
	{ cmd: false, shift: false },
	{ cmd: false, shift: true },
	{ cmd: true, shift: false },
	{ cmd: true, shift: true }
] as const;

function _probeEvent(key: string, cmd: boolean, shift: boolean) {
	const digit = /^\d$/.test(key);
	let eventKey = shift ? key.toUpperCase() : key;
	if (digit && shift) eventKey = SHIFTED_DIGITS[Number(key)];
	return {
		code: digit ? `Digit${key}` : `Key${key.toUpperCase()}`,
		key: eventKey,
		target: null,
		metaKey: cmd,
		ctrlKey: false,
		altKey: false,
		shiftKey: shift,
		repeat: false
	};
}

/** Every letter or digit chord the /performance router acts on (deck focus, grid jump on, no dial selected). */
export function listPerformanceLetterDigitChords(): PerformanceShortcutChord[] {
	const out: PerformanceShortcutChord[] = [];
	for (const key of PROBE_KEYS) {
		for (const { cmd, shift } of PROBE_MODIFIERS) {
			const action = resolvePerformanceShortcutAction(_probeEvent(key, cmd, shift), {
				gridJump: true,
				deckFocus: true
			});
			if (action === null) continue;
			const parts = [...(cmd ? ['Cmd'] : []), ...(shift ? ['Shift'] : []), key.toUpperCase()];
			out.push({ chord: parts.join('+'), action: action.kind });
		}
	}
	return out;
}

//------------------------------------------------------------------ report

/** The shipped command and preset data the report reads, plus the probed /performance shortcuts. */
export function shippedCoverageSources(): CoverageSources {
	return {
		commands: [
			...KEYMAP_COMMANDS.map((cmd) => ({
				id: cmd.id,
				source: 'keymap' as const,
				scopes: [cmd.scope],
				defaultKeys: []
			})),
			...[...PERFORMANCE_COMMAND_REGISTRY.values()].map((def) => ({
				id: def.id,
				source: 'registry' as const,
				scopes: def.scopes,
				defaultKeys: def.defaultKeys
			}))
		],
		presets: KEYMAP_PRESETS,
		performanceShortcuts: listPerformanceLetterDigitChords()
	};
}

function _hasChord(cmd: CoverageCommand, keymap: Keymap): boolean {
	return (keymap.bindings[cmd.id]?.length ?? 0) > 0 || cmd.defaultKeys.length > 0;
}

/** Build the coverage report from explicit sources; throws on duplicate ids. */
export function buildKeyboardCoverageReport(sources: CoverageSources): KeyboardCoverageReport {
	const ids = new Set<string>();
	for (const cmd of sources.commands) {
		if (ids.has(cmd.id)) throw new Error(`keyboard coverage: duplicate command id ${cmd.id}`);
		ids.add(cmd.id);
	}

	const noChord = Object.fromEntries(
		KEYMAP_PRESET_IDS.map((preset) => [
			preset,
			sources.commands.filter((c) => !_hasChord(c, sources.presets[preset])).map((c) => c.id)
		])
	) as Record<KeymapPresetId, string[]>;
	const performanceShortcuts = [...sources.performanceShortcuts];

	return {
		counts: {
			keymapCommands: sources.commands.filter((c) => c.source === 'keymap').length,
			registryCommands: sources.commands.filter((c) => c.source === 'registry').length,
			noChord: Object.fromEntries(KEYMAP_PRESET_IDS.map((p) => [p, noChord[p].length])) as Record<
				KeymapPresetId,
				number
			>,
			performanceLetterDigitChords: performanceShortcuts.length
		},
		noChord,
		performanceShortcuts
	};
}

/** The report for the shipped data. */
export function keyboardCoverageReport(): KeyboardCoverageReport {
	return buildKeyboardCoverageReport(shippedCoverageSources());
}

/**
 * Gap buckets whose count rose above its ceiling, each naming its current ids.
 * `performanceLetterDigitChords` is a floor (more shortcuts is better), so it breaches when it falls.
 */
export function coverageBreaches(
	report: KeyboardCoverageReport,
	ceilings: CoverageCeilings
): CoverageBreach[] {
	const out: CoverageBreach[] = [];
	for (const preset of KEYMAP_PRESET_IDS) {
		const count = report.counts.noChord[preset];
		const ceiling = ceilings.noChord[preset];
		if (count > ceiling)
			out.push({ bucket: `noChord.${preset}`, count, ceiling, ids: report.noChord[preset] });
	}
	const chords = report.counts.performanceLetterDigitChords;
	if (chords < ceilings.performanceLetterDigitChords) {
		out.push({
			bucket: 'performanceLetterDigitChords',
			count: chords,
			ceiling: ceilings.performanceLetterDigitChords,
			ids: report.performanceShortcuts.map((s) => s.chord)
		});
	}
	return out;
}
