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

export type CommandScope = 'global' | 'deck' | 'dial' | 'fader' | 'library';

export type PresetCommandId = `preset.run.${TransitionPresetId}`;

export type PerformanceCommandId = PresetCommandId;

export interface CommandRunContext {
	timeStamp: number;
	transitionPresets?: TransitionPresetHost;
}

export interface KeyboardCommandDefinition {
	id: PerformanceCommandId;
	label: string;
	defaultKeys: readonly string[];
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
		scope: 'global' as const,
		run(ctx: CommandRunContext) {
			if (!ctx.transitionPresets) {
				throw new TransitionPresetError('no_host', `${p.label}: no mixer host`);
			}
			runTransitionPreset(p.id, ctx.transitionPresets);
		}
	})
);

export const PERFORMANCE_COMMAND_REGISTRY =
	buildPerformanceCommandRegistry(PRESET_COMMAND_DEFINITIONS);

export function listPerformanceCommands(): readonly KeyboardCommandDefinition[] {
	return [...PERFORMANCE_COMMAND_REGISTRY.values()];
}

export function runCommand(id: PerformanceCommandId, ctx: CommandRunContext): void {
	const def = PERFORMANCE_COMMAND_REGISTRY.get(id);
	if (!def) {
		throw new Error(`unknown command: ${id}`);
	}
	def.run(ctx);
}

export { _resetTransitionPresetsForTests } from '$lib/rb/transition-presets';
