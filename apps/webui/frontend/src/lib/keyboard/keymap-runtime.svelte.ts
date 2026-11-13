/**
 * Live keyboard layout preset runtime (#10351 / KEYB-11488).
 */
import { api, unwrap } from '$lib/api/client';
import { subscribeKind } from '$lib/api/events-bus';
import { parseKeyboardPrefs } from '$lib/keyboard/keyboard-prefs';
import { isTextEntryTarget } from '$lib/keyboard/text-entry-target';
import { deckStates, mixerState } from '$lib/rb/audio-engine.svelte';
import type { DeckId } from '$lib/rb/deck-id';
import { setDeckLayoutMode } from '$lib/rb/prefs.svelte';
import { pressHotCueSlot, type Notify } from '$lib/rb/midi/controller-pad-runtime.svelte';
import { handleMidiAction } from '$lib/rb/midi/action-glue.svelte';
import type { PerformanceShortcutHandlers } from '$lib/rb/performance-shortcut-routing';
import { isNativeInteractiveTarget } from '$lib/rb/performance-hotkeys-target';
import { runPerformanceCommandFromUi, uiPrefs } from '$lib/rb/performance-ipc.svelte';
import { isSettingsOpen } from '$lib/settings/overlay.svelte';

import { installKeyboardAgentHandle } from './keyboard-agent-handle';
import { KEYMAP_COMMANDS, keymapCommandTarget } from './keymap-commands';
import {
	chordFromEvent,
	effectiveKeymap,
	getKeymapPreset,
	keymapOwnsKeydown,
	legacyPerformanceChordsActive,
	resolveKeymapKeydown,
	type Keymap,
	type KeymapOverrides
} from './keymap';
import type { KeymapPresetId } from './presets/index';

export interface KeyboardKeymapState {
	preset: KeymapPresetId;
	overrides: KeymapOverrides;
}

const keyboardState = $state.raw<KeyboardKeymapState>({
	preset: 'opendj',
	overrides: {}
});

function instantDoublePartner(deck: DeckId): DeckId {
	if (deck === 1) return 2;
	if (deck === 2) return 1;
	if (deck === 3) return 4;
	return 3;
}

function clamp01(value: number): number {
	return Math.min(1, Math.max(0, value));
}

function runMixerDelta(
	target: 'fader' | 'eq' | 'crossfader',
	deck: DeckId | undefined,
	band: 'low' | 'mid' | 'high' | undefined,
	delta: number
): void {
	if (target === 'crossfader') {
		const next = clamp01(mixerState.crossfader + delta);
		void runPerformanceCommandFromUi({ type: 'crossfader', value: next });
		return;
	}
	if (deck === undefined) return;
	const channel = mixerState.channels[deck];
	if (target === 'fader') {
		void runPerformanceCommandFromUi({
			type: 'fader',
			deck,
			value: clamp01(channel.fader + delta)
		});
		return;
	}
	if (band === undefined) return;
	const key = `eq_${band}` as 'eq_low' | 'eq_mid' | 'eq_high';
	const next = clamp01(channel[key] + delta);
	void runPerformanceCommandFromUi({ type: 'eq', deck, band, value: next });
}

const silentNotify: Notify = () => {};

/** Run one keymap command's target, as its chord press does; legacy targets need `legacy`. */
export function runKeymapTarget(
	commandId: string,
	pressT0Ms: number,
	legacy: PerformanceShortcutHandlers | null
): void {
	const target = keymapCommandTarget(commandId);
	if (target === null) return;
	if (target.kind === 'midi') {
		handleMidiAction(
			target.action,
			{ kind: 'button', pressed: true, velocity: 127 },
			undefined,
			pressT0Ms
		);
		return;
	}
	if (target.kind === 'hot_cue') {
		pressHotCueSlot(target.deck, target.slot, target.mode, silentNotify, pressT0Ms);
		return;
	}
	if (target.kind === 'mixer') {
		runMixerDelta(target.target, target.deck, target.band, target.delta);
		return;
	}
	if (target.kind === 'legacy') {
		if (legacy === null) throw new Error(`keymap command ${commandId} needs shortcut handlers`);
		switch (target.name) {
			case 'transport.toggle':
				legacy.toggleRecentPlay(pressT0Ms, false);
				break;
			case 'transport.toggle_quantized':
				legacy.toggleRecentPlay(pressT0Ms, true);
				break;
			case 'loop.double_last':
				legacy.resizeLast(2);
				break;
			case 'loop.halve_last':
				legacy.resizeLast(0.5);
				break;
			case 'loop.exit_last':
				legacy.exitLast();
				break;
			case 'comment.pin':
				legacy.armPinPlacement();
				break;
			case 'wave.zoom_out':
				legacy.waveZoomStep('out');
				break;
			case 'wave.zoom_in':
				legacy.waveZoomStep('in');
				break;
			case 'layout.deck_less':
				setDeckLayoutMode('less');
				break;
			case 'layout.deck_more':
				setDeckLayoutMode('more');
				break;
			case 'grid.jump_back_4':
				legacy.gridJump?.(-4);
				break;
			case 'grid.jump_forward_4':
				legacy.gridJump?.(4);
				break;
			case 'grid.jump_back_8':
				legacy.gridJump?.(-8);
				break;
			case 'grid.jump_forward_8':
				legacy.gridJump?.(8);
				break;
			default: {
				const _exhaustive: never = target.name;
				throw new Error(`Unhandled legacy keymap command: ${String(_exhaustive)}`);
			}
		}
		return;
	}
	const deck = target.deck;
	if (target.op === 'seek_start') {
		void runPerformanceCommandFromUi({ type: 'seek', deck, position_ms: 0 }, pressT0Ms);
	} else if (target.op === 'master') {
		void runPerformanceCommandFromUi({ type: 'master', deck }, pressT0Ms);
	} else if (target.op === 'keylock') {
		const enabled = !deckStates[deck].master_tempo_enabled;
		void runPerformanceCommandFromUi({ type: 'master_tempo', deck, enabled }, pressT0Ms);
	} else if (target.op === 'loop_in') {
		void runPerformanceCommandFromUi({ type: 'loop_in', deck }, pressT0Ms);
	} else if (target.op === 'loop_out') {
		void runPerformanceCommandFromUi({ type: 'loop_out', deck }, pressT0Ms);
	} else if (target.op === 'reloop_exit') {
		const loop = deckStates[deck].loop;
		if (loop?.engaged) {
			handleMidiAction(
				{ type: 'deck_loop_exit', deck },
				{ kind: 'button', pressed: true, velocity: 127 },
				undefined,
				pressT0Ms
			);
		} else {
			void runPerformanceCommandFromUi({ type: 'reloop', deck }, pressT0Ms);
		}
	} else if (target.op === 'instant_double') {
		void runPerformanceCommandFromUi(
			{ type: 'deck_move', from: instantDoublePartner(deck), to: deck, mode: 'copy' },
			pressT0Ms
		);
	}
}

export function refreshKeyboardPrefFromServer(): void {
	keyboardState.preset = uiPrefs.keyboard.preset;
	keyboardState.overrides = uiPrefs.keyboard.overrides;
}

/** Agent or another tab PUT keyboard prefs: re-read disk so overlay and keymap follow without reload. */
export async function hydrateKeyboardPrefFromDisk(): Promise<void> {
	try {
		const body = (await unwrap(api.GET('/api/v1/ui-prefs'))) as { keyboard?: unknown };
		if (body.keyboard !== undefined) {
			const keyboard = parseKeyboardPrefs(body.keyboard);
			if (keyboard !== undefined) {
				uiPrefs.keyboard = keyboard;
			}
		}
		refreshKeyboardPrefFromServer();
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		console.error(`[keyboard] ui_prefs re-read failed: ${message}`);
	}
}

export function activeKeyboardPreset(): KeymapPresetId {
	return keyboardState.preset;
}

export function keyboardKeymapState(): KeyboardKeymapState {
	return keyboardState;
}

/** The keymap a chord press resolves against right now (preset plus overrides). */
export function activeKeyboardKeymap(): Keymap {
	return effectiveKeymap(getKeymapPreset(keyboardState.preset), keyboardState.overrides);
}

export function installKeyboardKeymapRuntime(): () => void {
	refreshKeyboardPrefFromServer();
	const off = subscribeKind('ui_prefs', () => {
		void hydrateKeyboardPrefFromDisk();
	});
	const uninstallAgentHandle = installKeyboardAgentHandle({
		commands: KEYMAP_COMMANDS,
		activeKeymap: activeKeyboardKeymap,
		runKeymapTarget
	});
	return () => {
		off();
		uninstallAgentHandle();
	};
}

/**
 * #14100: plain Space and plain Enter activate a focused native control (button, link, select,
 * checkbox), so no keymap binding may take them there. Modifier chords (Cmd+Enter) still route.
 */
const NATIVE_ACTIVATION_CHORDS: ReadonlySet<string> = new Set(['Space', 'Enter']);

function nativeControlOwnsChord(chord: string | null, target: EventTarget | null): boolean {
	return chord !== null && NATIVE_ACTIVATION_CHORDS.has(chord) && isNativeInteractiveTarget(target);
}

export function handleKeymapKeydown(
	e: KeyboardEvent,
	legacy: PerformanceShortcutHandlers
): boolean {
	if (isSettingsOpen() || isTextEntryTarget(e.target)) return false;
	const chord = chordFromEvent(e);
	if (nativeControlOwnsChord(chord, e.target)) return false;

	const state = keyboardState;
	if (state.preset === 'opendj' && Object.keys(state.overrides).length === 0) {
		return false;
	}

	const keymap = activeKeyboardKeymap();
	let commandId: string | null = null;

	if (state.preset === 'opendj') {
		if (chord === null || !keymapOwnsKeydown(state, chord)) return false;
		commandId = resolveKeymapKeydown(keymap, e, { activeScopes: ['global', 'deck'] });
	} else {
		commandId = resolveKeymapKeydown(keymap, e, { activeScopes: ['global', 'deck'] });
	}

	if (commandId === null) return false;
	e.preventDefault();
	if (e.repeat) return true;
	runKeymapTarget(commandId, e.timeStamp, legacy);
	return true;
}

export { legacyPerformanceChordsActive };
