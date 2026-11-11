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
	COMMANDED_KEY_COMMANDS,
	runCommandedCommand,
	type CommandedCommandId,
	type CommandedKeyDeps
} from '$lib/rb/commanded-keys';

export type CommandScope = 'global' | 'deck' | 'dial' | 'fader' | 'library';

export type PresetCommandId = `preset.run.${TransitionPresetId}`;

export type PerformanceCommandId = PresetCommandId | CommandedCommandId;

export interface CommandRunContext {
	timeStamp: number;
	transitionPresets?: TransitionPresetHost;
	commanded?: CommandedKeyDeps;
}

export interface KeyboardCommandDefinition {
	id: PerformanceCommandId;
	label: string;
	defaultKeys: readonly string[];
	/** Every scope the command acts in; `scope` is the first. */
	scopes: readonly CommandScope[];
	scope: CommandScope;
	run: (ctx: CommandRunContext) => void;
}

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

export const PERFORMANCE_COMMAND_REGISTRY = buildPerformanceCommandRegistry([
	...PRESET_COMMAND_DEFINITIONS,
	...COMMANDED_COMMAND_DEFINITIONS
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

export function runCommand(id: PerformanceCommandId, ctx: CommandRunContext): void {
	const def = PERFORMANCE_COMMAND_REGISTRY.get(id);
	if (!def) {
		throw new Error(`unknown command: ${id}`);
	}
	def.run(ctx);
}

export { _resetTransitionPresetsForTests } from '$lib/rb/transition-presets';
