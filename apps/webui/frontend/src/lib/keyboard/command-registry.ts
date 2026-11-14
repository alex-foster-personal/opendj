/**
 * Performance keyboard/command registry (#9394).
 *
 * Draft PR #9549 adds the same file with transport commands. Whichever merges second
 * must union both definition lists and merge CommandRunContext fields.
 */

import {
	TRANSITION_PRESETS,
	runTransitionPreset,
	TransitionPresetError,
	type TransitionPresetHost,
	type TransitionPresetId
} from '$lib/rb/transition-presets';
import {
	COMMANDED_DURATION_COMMANDS,
	COMMANDED_KEY_COMMANDS,
	runCommandedCommand,
	runCommandedDurationCommand,
	type CommandedCommandId,
	type CommandedKeyDeps
} from '$lib/rb/commanded-keys';
import type { SelectorDirection, SelectorTree } from './selector-tree';

export type CommandScope = 'global' | 'deck' | 'dial' | 'fader' | 'library';

export type PresetCommandId = `preset.run.${TransitionPresetId}`;

export type SelectorCommandId =
	`selector.move.${SelectorDirection}` | 'selector.drill-in' | 'selector.drill-out';

/** #13708: operate the selected selector leaf (Enter, Shift+Enter #14522, arrows). */
export const SELECTOR_LEAF_COMMAND_IDS = [
	'selector.activate',
	'selector.shift-activate',
	'selector.adjust.up',
	'selector.adjust.down'
] as const;

export type SelectorLeafCommandId = (typeof SELECTOR_LEAF_COMMAND_IDS)[number];

/** The live selector leaf the leaf commands operate (performance-hotkeys.ts). */
export interface SelectorLeafHost {
	/** Press the leaf's control; `shift` presses it as a Shift+click (#14522). */
	activate(shift: boolean): string;
	adjust(direction: 1 | -1): string;
}

export type PerformanceCommandId =
	PresetCommandId | CommandedCommandId | SelectorCommandId | SelectorLeafCommandId;

export interface CommandRunContext {
	timeStamp: number;
	transitionPresets?: TransitionPresetHost;
	commanded?: CommandedKeyDeps;
	selector?: Pick<SelectorTree, 'move' | 'drillIn' | 'drillOut'>;
	selectorLeaf?: SelectorLeafHost;
}

export interface KeyboardCommandDefinition {
	id: PerformanceCommandId;
	label: string;
	defaultKeys: readonly string[];
	/** Every scope the command acts in; `scope` is the first. */
	scopes: readonly CommandScope[];
	scope: CommandScope;
	run: (ctx: CommandRunContext) => string | void;
}

export type CommandMetadata = Omit<KeyboardCommandDefinition, 'run'>;

export function presetCommandId(id: TransitionPresetId): PresetCommandId {
	return `preset.run.${id}`;
}

export function buildPerformanceCommandRegistry(
	defs: readonly KeyboardCommandDefinition[]
): ReadonlyMap<PerformanceCommandId, KeyboardCommandDefinition> {
	const map = new Map<PerformanceCommandId, KeyboardCommandDefinition>();
	for (const def of defs) {
		if (map.has(def.id)) {
			throw new Error(`duplicate command id: ${def.id}`);
		}
		if (def.label.trim().length === 0) {
			throw new Error(`empty label for command: ${def.id}`);
		}
		map.set(def.id, def);
	}
	return map;
}

const PRESET_COMMAND_DEFINITIONS: readonly KeyboardCommandDefinition[] = TRANSITION_PRESETS.map(
	(p) => ({
		id: presetCommandId(p.id),
		label: `Transition preset: ${p.label}`,
		defaultKeys: [] as const,
		scopes: ['global'] as const,
		scope: 'global' as const,
		run(ctx: CommandRunContext) {
			if (!ctx.transitionPresets) {
				throw new TransitionPresetError('no_host', `${p.label}: no mixer host`);
			}
			runTransitionPreset(p.id, ctx.transitionPresets);
		}
	})
);

/** dial.position.1-9 and dial.jump.1-9 (#9453); act in dial and fader, key handler, Cmd-K, __mdtKnobSelection.command. */
const COMMANDED_COMMAND_DEFINITIONS: readonly KeyboardCommandDefinition[] =
	COMMANDED_KEY_COMMANDS.map((c) => ({
		id: c.id,
		label: c.label,
		defaultKeys: c.defaultKeys,
		scopes: c.scopes,
		scope: c.scopes[0],
		run(ctx: CommandRunContext) {
			if (!ctx.commanded) {
				throw new Error(`${c.id}: no dial or fader host`);
			}
			runCommandedCommand(c, ctx.commanded);
		}
	}));

/** dial.duration.longer and dial.duration.shorter (#9455): = and - step the commanded move length. */
const COMMANDED_DURATION_COMMAND_DEFINITIONS: readonly KeyboardCommandDefinition[] =
	COMMANDED_DURATION_COMMANDS.map((c) => ({
		id: c.id,
		label: c.label,
		defaultKeys: c.defaultKeys,
		scopes: c.scopes,
		scope: c.scopes[0],
		run(ctx: CommandRunContext) {
			if (!ctx.commanded) {
				throw new Error(`${c.id}: no dial or fader host`);
			}
			runCommandedDurationCommand(c, ctx.commanded);
		}
	}));

function _selector(ctx: CommandRunContext): NonNullable<CommandRunContext['selector']> {
	if (!ctx.selector) throw new Error('Selector command: no selector host; provide ctx.selector');
	return ctx.selector;
}

/** #9395: the /performance keydown binds these keys (performance-shortcut-routing.ts). */
const SELECTOR_MOVE_KEYS = { up: 'W', down: 'S', left: 'A', right: 'D' } as const;

const SELECTOR_COMMAND_DEFINITIONS: readonly KeyboardCommandDefinition[] = [
	...(['up', 'down', 'left', 'right'] as const).map((direction) => ({
		id: `selector.move.${direction}` as const,
		label: `Move selector ${direction}`,
		defaultKeys: [SELECTOR_MOVE_KEYS[direction]],
		scopes: ['global'] as const,
		scope: 'global' as const,
		run: (ctx: CommandRunContext) => _selector(ctx).move(direction)
	})),
	{
		id: 'selector.drill-in',
		label: 'Drill into selector',
		defaultKeys: ['E'],
		scopes: ['global'],
		scope: 'global',
		run: (ctx) => _selector(ctx).drillIn()
	},
	{
		id: 'selector.drill-out',
		label: 'Drill out of selector',
		defaultKeys: ['Q'],
		scopes: ['global'],
		scope: 'global',
		run: (ctx) => _selector(ctx).drillOut()
	}
];

function _selectorLeaf(ctx: CommandRunContext): SelectorLeafHost {
	if (!ctx.selectorLeaf)
		throw new Error('Selector leaf command: no selected leaf; provide ctx.selectorLeaf');
	return ctx.selectorLeaf;
}

/** #13708: bound by the /performance keydown only while a selector leaf is selected. */
const SELECTOR_LEAF_COMMAND_DEFINITIONS: readonly KeyboardCommandDefinition[] = [
	{
		id: 'selector.activate',
		label: 'Activate selected control',
		defaultKeys: ['Enter'],
		scopes: ['global'],
		scope: 'global',
		run: (ctx) => _selectorLeaf(ctx).activate(false)
	},
	{
		id: 'selector.shift-activate',
		label: 'Shift-click selected control',
		defaultKeys: ['Shift+Enter'],
		scopes: ['global'],
		scope: 'global',
		run: (ctx) => _selectorLeaf(ctx).activate(true)
	},
	{
		id: 'selector.adjust.up',
		label: 'Adjust selected control up',
		defaultKeys: ['ArrowUp', 'ArrowRight'],
		scopes: ['global'],
		scope: 'global',
		run: (ctx) => _selectorLeaf(ctx).adjust(1)
	},
	{
		id: 'selector.adjust.down',
		label: 'Adjust selected control down',
		defaultKeys: ['ArrowDown', 'ArrowLeft'],
		scopes: ['global'],
		scope: 'global',
		run: (ctx) => _selectorLeaf(ctx).adjust(-1)
	}
];

export const PERFORMANCE_COMMAND_REGISTRY = buildPerformanceCommandRegistry([
	...PRESET_COMMAND_DEFINITIONS,
	...COMMANDED_COMMAND_DEFINITIONS,
	...COMMANDED_DURATION_COMMAND_DEFINITIONS,
	...SELECTOR_COMMAND_DEFINITIONS,
	...SELECTOR_LEAF_COMMAND_DEFINITIONS
]);

export function listPerformanceCommands(): readonly KeyboardCommandDefinition[] {
	return [...PERFORMANCE_COMMAND_REGISTRY.values()];
}

/** The registered commands that act in `scope` (a dial/fader selection gives 'dial' or 'fader'). */
export function performanceCommandsInScope(
	scope: CommandScope
): readonly KeyboardCommandDefinition[] {
	return [...PERFORMANCE_COMMAND_REGISTRY.values()].filter((def) => def.scopes.includes(scope));
}

export function listPerformanceCommandMetadata(): readonly CommandMetadata[] {
	return listPerformanceCommands().map(({ id, label, scopes, scope, defaultKeys }) => ({
		id,
		label,
		scopes: [...scopes],
		scope,
		defaultKeys: [...defaultKeys]
	}));
}

export function runCommand(id: PerformanceCommandId, ctx: CommandRunContext): string | void {
	const def = PERFORMANCE_COMMAND_REGISTRY.get(id);
	if (!def) {
		throw new Error(`unknown command: ${id}`);
	}
	return def.run(ctx);
}

export { _resetTransitionPresetsForTests } from '$lib/rb/transition-presets';
