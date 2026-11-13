/**
 * Keyboard coverage report (#13689, epic #10346): how far the app is from full keyboard control.
 *
 * Pure data, no DOM. The command universe is every `KEYMAP_COMMANDS` id plus every
 * `PERFORMANCE_COMMAND_REGISTRY` id. For each command it asks two questions:
 * - chord: does the preset bind it (registry commands also count their own `defaultKeys`)?
 * - palette row: does the Cmd-K bar offer it? A row is a `PALETTE_REGISTRY` entry with a
 *   runner (entries with `run: null` are registered but not offered), matched by id or
 *   through `PALETTE_ALIASES`; registry commands in a dial or fader scope also count,
 *   because CommandBarV2 lists those through `registryCommandCandidates`.
 * The /performance letter and digit shortcuts are counted separately by probing the pure
 * router with synthetic key events.
 */
import { PALETTE_REGISTRY } from '$lib/palette/commands';
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

export interface CoveragePaletteEntry {
	id: string;
	runnable: boolean;
}

export interface CoverageSources {
	commands: readonly CoverageCommand[];
	presets: Readonly<Record<KeymapPresetId, Keymap>>;
	palette: readonly CoveragePaletteEntry[];
	aliases: Readonly<Record<string, string>>;
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
	noPaletteRow: number;
	noKeyboardPath: number;
	performanceLetterDigitChords: number;
}

export interface KeyboardCoverageReport {
	counts: KeyboardCoverageCounts;
	noChord: Record<KeymapPresetId, string[]>;
	noPaletteRow: string[];
	/** No chord in the default (opendj) preset and no palette row. */
	noKeyboardPath: string[];
	performanceShortcuts: PerformanceShortcutChord[];
}

export interface CoverageBreach {
	bucket: string;
	count: number;
	ceiling: number;
	ids: string[];
}

export type CoverageCeilings = Omit<KeyboardCoverageCounts, 'keymapCommands' | 'registryCommands'>;

//------------------------------------------------------------------ alias map

/**
 * Command family (see `commandFamily`) to the palette id that offers the same action.
 * Per-deck families map to deck-argument palette rows, which act on the bar's target deck.
 */
export const PALETTE_ALIASES: Readonly<Record<string, string>> = {
	'transport.toggle': 'deck.play',
	'loop.double_last': 'loop.double',
	'loop.halve_last': 'loop.halve',
	'loop.exit_last': 'deck.loop.exit',
	'wave.zoom_out': 'waveform.zoom_out',
	'wave.zoom_in': 'waveform.zoom_in',
	'comment.pin': 'feedback.pin',
	'layout.deck_less': 'layout.less',
	'layout.deck_more': 'layout.more',
	'deck.*.play': 'deck.play',
	'deck.*.beat_sync': 'deck.beat_sync',
	'deck.*.sync_master': 'deck.master',
	'deck.*.load': 'library.load_deck',
	'deck.*.instant_double': 'library.instant_double',
	'deck.*.hot_cue.*': 'hot_cue.trigger',
	'deck.*.hot_cue_set.*': 'hot_cue.save',
	'deck.*.hot_cue_clear.*': 'hot_cue.clear',
	'mixer.eq_low_up.*': 'mixer.eq',
	'mixer.eq_low_down.*': 'mixer.eq',
	'mixer.eq_mid_up.*': 'mixer.eq',
	'mixer.eq_mid_down.*': 'mixer.eq',
	'mixer.eq_high_up.*': 'mixer.eq',
	'mixer.eq_high_down.*': 'mixer.eq',
	'dial.position.*': 'dial.position',
	'dial.jump.*': 'dial.jump'
};

const PALETTE_ROW_REGISTRY_SCOPES: ReadonlySet<CommandScope> = new Set(['dial', 'fader']);

/** Collapse a deck number, hot cue slot or channel number to `*` (`deck.1.hot_cue.A` -> `deck.*.hot_cue.*`). */
export function commandFamily(id: string): string {
	return id
		.replace(/^deck\.\d\./, 'deck.*.')
		.replace(/\.[A-H]$/, '.*')
		.replace(/\.\d$/, '.*');
}

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

/** The shipped command, preset and palette data the report reads. */
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
		palette: PALETTE_REGISTRY.commands.map((c) => ({ id: c.id, runnable: c.run !== null })),
		aliases: PALETTE_ALIASES,
		performanceShortcuts: listPerformanceLetterDigitChords()
	};
}

function _hasChord(cmd: CoverageCommand, keymap: Keymap): boolean {
	return (keymap.bindings[cmd.id]?.length ?? 0) > 0 || cmd.defaultKeys.length > 0;
}

function _hasPaletteRow(
	cmd: CoverageCommand,
	runnable: ReadonlySet<string>,
	sources: CoverageSources
): boolean {
	if (cmd.source === 'registry' && cmd.scopes.some((s) => PALETTE_ROW_REGISTRY_SCOPES.has(s)))
		return true;
	if (runnable.has(cmd.id)) return true;
	const alias = sources.aliases[commandFamily(cmd.id)];
	return alias !== undefined && runnable.has(alias);
}

/** Build the coverage report from explicit sources; throws on duplicate ids or a stale alias. */
export function buildKeyboardCoverageReport(sources: CoverageSources): KeyboardCoverageReport {
	const ids = new Set<string>();
	for (const cmd of sources.commands) {
		if (ids.has(cmd.id)) throw new Error(`keyboard coverage: duplicate command id ${cmd.id}`);
		ids.add(cmd.id);
	}
	const paletteIds = new Set(sources.palette.map((p) => p.id));
	const families = new Set(sources.commands.map((c) => commandFamily(c.id)));
	for (const [family, target] of Object.entries(sources.aliases)) {
		if (!families.has(family))
			throw new Error(`keyboard coverage: alias ${family} matches no command`);
		if (!paletteIds.has(target))
			throw new Error(`keyboard coverage: alias ${family} names unknown palette id ${target}`);
	}
	const runnable = new Set(sources.palette.filter((p) => p.runnable).map((p) => p.id));

	const noChord = Object.fromEntries(
		KEYMAP_PRESET_IDS.map((preset) => [
			preset,
			sources.commands.filter((c) => !_hasChord(c, sources.presets[preset])).map((c) => c.id)
		])
	) as Record<KeymapPresetId, string[]>;
	const noPaletteRow = sources.commands
		.filter((c) => !_hasPaletteRow(c, runnable, sources))
		.map((c) => c.id);
	const noRow = new Set(noPaletteRow);
	const noKeyboardPath = noChord.opendj.filter((id) => noRow.has(id));
	const performanceShortcuts = [...sources.performanceShortcuts];

	return {
		counts: {
			keymapCommands: sources.commands.filter((c) => c.source === 'keymap').length,
			registryCommands: sources.commands.filter((c) => c.source === 'registry').length,
			noChord: Object.fromEntries(KEYMAP_PRESET_IDS.map((p) => [p, noChord[p].length])) as Record<
				KeymapPresetId,
				number
			>,
			noPaletteRow: noPaletteRow.length,
			noKeyboardPath: noKeyboardPath.length,
			performanceLetterDigitChords: performanceShortcuts.length
		},
		noChord,
		noPaletteRow,
		noKeyboardPath,
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
	for (const bucket of ['noPaletteRow', 'noKeyboardPath'] as const) {
		const count = report.counts[bucket];
		if (count > ceilings[bucket])
			out.push({ bucket, count, ceiling: ceilings[bucket], ids: report[bucket] });
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
