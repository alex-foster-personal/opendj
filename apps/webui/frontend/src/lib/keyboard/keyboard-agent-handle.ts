/**
 * Agent parity for the keyboard (#13690, epic #10346).
 *
 * `window.__odjKeyboard` lists every keymap and performance registry command and runs
 * any of them by id, without synthesising key events. Keymap commands go through the
 * same target runner a chord press uses. Registry commands go through `runCommand`.
 * A command whose host is missing (no live selector, no selected leaf or dial, no shortcut
 * handlers) returns a named refusal. An unknown id throws. `selector()` (#12267) reads
 * the live selection as plain JSON: id, node kind and the selector commands that would act.
 */
import type { KnobSelectionBridge } from '$lib/rb/knob-control.svelte';
import type { PerformanceShortcutHandlers } from '$lib/rb/performance-shortcut-routing';
import { TransitionPresetError } from '$lib/rb/transition-presets';

import {
	listPerformanceCommandMetadata,
	runCommand,
	SELECTOR_LEAF_COMMAND_IDS,
	type CommandMetadata,
	type CommandRunContext,
	type CommandScope,
	type PerformanceCommandId,
	type SelectorLeafHost
} from './command-registry';
import type { Keymap } from './keymap';
import { isSelectorLeafRefusal, type SelectorLeafRefusalCode } from './selector-activate';
import { keymapCommandTarget, type KeymapCommand } from './keymap-commands';
import {
	describeSelectorSelection,
	snapshotSelectorNodes,
	type SelectorDebugHandle,
	type SelectorStateSnapshot
} from './selector-register';
import type { SelectorTree } from './selector-tree';

export type KeyboardCommandSource = 'keymap' | 'registry';

export interface KeyboardCommandListing {
	id: string;
	label: string;
	scope: CommandScope;
	source: KeyboardCommandSource;
	chords: string[];
}

export type KeyboardRefusal =
	| 'no_selector'
	| 'no_leaf_selected'
	| 'no_dial_selected'
	| 'no_shortcut_handlers'
	| SelectorLeafRefusalCode
	| string;

export type KeyboardRunResult =
	| { ok: true; id: string; selected: string | null }
	| { ok: false; id: string; refusal: KeyboardRefusal };

export interface KeyboardAgentHandle {
	list(): KeyboardCommandListing[];
	run(id: string): KeyboardRunResult;
	selector(): SelectorStateSnapshot;
}

export type KeyboardSelectorHost = Pick<SelectorTree, 'move' | 'drillIn' | 'drillOut'> & {
	selected(): string | null;
	/** #13989: the selected leaf Enter and the arrows operate, or null while no leaf is selected. */
	selectedLeaf(): SelectorLeafHost | null;
};
export type KeyboardSelectorProvider = () => KeyboardSelectorHost | null;
export type KeyboardShortcutHandlersProvider = () => PerformanceShortcutHandlers | null;

/** What the keymap runtime hands the handle: its commands, live keymap and target runner. */
export interface KeyboardKeymapRuntimeDeps {
	commands: readonly KeymapCommand[];
	activeKeymap: () => Keymap;
	runKeymapTarget: (
		commandId: string,
		pressT0Ms: number,
		legacy: PerformanceShortcutHandlers | null
	) => void;
}

declare global {
	interface Window {
		__odjKeyboard?: KeyboardAgentHandle;
		__mdtKnobSelection?: KnobSelectionBridge;
	}
}

//----- provider seams

/** The tree `window.__odjSelector.start()` built, or null while none is started. */
function _odjSelectorHost(): KeyboardSelectorHost | null {
	const handle: SelectorDebugHandle | undefined = globalThis.window?.__odjSelector;
	if (handle === undefined) return null;
	try {
		handle.selected();
	} catch {
		// selected() throws until start() has run: that is "no live selector", a refusal.
		return null;
	}
	return {
		selected: () => handle.selected(),
		// The debug tree has no page leaf to press or step: leaf commands are refused.
		selectedLeaf: () => null,
		move: (direction) => handle.move(direction),
		drillIn: () => handle.drillIn(),
		drillOut: () => handle.drillOut()
	};
}

let selectorProvider: KeyboardSelectorProvider = _odjSelectorHost;
let shortcutHandlersProvider: KeyboardShortcutHandlersProvider = () => null;

/** Point selector commands at a live selector session; null restores the `__odjSelector` tree. */
export function setKeyboardSelectorProvider(provider: KeyboardSelectorProvider | null): void {
	selectorProvider = provider ?? _odjSelectorHost;
}

/** Give legacy keymap targets (transport toggle, loop resize) their handlers; null removes them. */
export function setKeyboardShortcutHandlersProvider(
	provider: KeyboardShortcutHandlersProvider | null
): void {
	shortcutHandlersProvider = provider ?? (() => null);
}

//----- handle

function _refused(id: string, refusal: KeyboardRefusal): KeyboardRunResult {
	return { ok: false, id, refusal };
}

const LEAF_COMMAND_IDS: readonly string[] = SELECTOR_LEAF_COMMAND_IDS;

/** Build the list/run handle over a keymap runtime and the performance command registry. */
export function createKeyboardAgentHandle(deps: KeyboardKeymapRuntimeDeps): KeyboardAgentHandle {
	const registry = (): Map<string, CommandMetadata> =>
		new Map(listPerformanceCommandMetadata().map((def) => [def.id, def]));

	function list(): KeyboardCommandListing[] {
		const keymap = deps.activeKeymap();
		const keymapRows = deps.commands.map((cmd) => ({
			id: cmd.id,
			label: cmd.label,
			scope: cmd.scope,
			source: 'keymap' as const,
			chords: [...(keymap.bindings[cmd.id] ?? [])]
		}));
		const registryRows = [...registry().values()].map((def) => ({
			id: def.id,
			label: def.label,
			scope: def.scope,
			source: 'registry' as const,
			chords: [...def.defaultKeys]
		}));
		return [...keymapRows, ...registryRows];
	}

	function runKeymap(id: string): KeyboardRunResult {
		const target = keymapCommandTarget(id);
		if (target === null) throw new Error(`keymap command has no target: ${id}`);
		const handlers = target.kind === 'legacy' ? shortcutHandlersProvider() : null;
		if (target.kind === 'legacy' && handlers === null) return _refused(id, 'no_shortcut_handlers');
		deps.runKeymapTarget(id, performance.now(), handlers);
		return { ok: true, id, selected: null };
	}

	function runRegistry(
		id: PerformanceCommandId,
		scopes: readonly CommandScope[]
	): KeyboardRunResult {
		if (id.startsWith('selector.')) {
			const selector = selectorProvider();
			if (selector === null) return _refused(id, 'no_selector');
			let ctx: CommandRunContext = { timeStamp: performance.now(), selector };
			if (LEAF_COMMAND_IDS.includes(id)) {
				const selectorLeaf = selector.selectedLeaf();
				if (selectorLeaf === null) return _refused(id, 'no_leaf_selected');
				ctx = { timeStamp: ctx.timeStamp, selectorLeaf };
			}
			let selected: unknown;
			try {
				selected = runCommand(id, ctx);
			} catch (error) {
				// #14522: Enter on a leaf with nothing to press (the jog) is a refusal, not a crash.
				if (isSelectorLeafRefusal(error)) return _refused(id, error.code);
				throw error;
			}
			if (typeof selected !== 'string') {
				throw new Error(`selector command ${id} did not return a selected node id`);
			}
			return { ok: true, id, selected };
		}
		if (scopes.includes('dial') || scopes.includes('fader')) {
			const bridge = globalThis.window?.__mdtKnobSelection;
			if (bridge === undefined || bridge.get().length === 0)
				return _refused(id, 'no_dial_selected');
			bridge.command(id);
			return { ok: true, id, selected: null };
		}
		try {
			runCommand(id, { timeStamp: performance.now() });
		} catch (error) {
			if (error instanceof TransitionPresetError) return _refused(id, error.code);
			throw error;
		}
		return { ok: true, id, selected: null };
	}

	function run(id: string): KeyboardRunResult {
		if (deps.commands.some((cmd) => cmd.id === id)) return runKeymap(id);
		const def = registry().get(id);
		if (def === undefined) throw new Error(`unknown keyboard command: ${id}`);
		return runRegistry(def.id, def.scopes);
	}

	function selector(): SelectorStateSnapshot {
		const selected = selectorProvider()?.selected() ?? null;
		return describeSelectorSelection(snapshotSelectorNodes(), selected);
	}

	return { list, run, selector };
}

/** Install `window.__odjKeyboard`; the returned function removes it. */
export function installKeyboardAgentHandle(deps: KeyboardKeymapRuntimeDeps): () => void {
	if (typeof window === 'undefined') return () => {};
	const handle = createKeyboardAgentHandle(deps);
	window.__odjKeyboard = handle;
	return () => {
		if (window.__odjKeyboard === handle) delete window.__odjKeyboard;
	};
}
