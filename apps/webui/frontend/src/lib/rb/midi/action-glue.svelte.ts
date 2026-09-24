/**
 * MIDI action glue for the P0 controller build (build unit: midi core).
 * Rune module (.svelte.ts): consumes the engine's $state stores and runs
 * the LED-feedback $effect root.
 *
 * Maps MidiAction emissions from webmidi.svelte.ts onto the audio engine.
 *
 * IPC NOTE (worktree base 6bb35aa): src/lib/rb/performance-ipc.svelte.ts
 * (Codex's typed command path) does NOT exist at this commit, so deck
 * COMMANDS call the engine directly - but each command goes through ONE
 * clearly-marked adapter function below (REBASE ADAPTER block) so the
 * rebase onto the landed IPC is a per-function 5-line change. Mixer knobs /
 * faders / pitch are continuous controls, not commands, and go direct to
 * the engine setters permanently by design.
 *
 * Requirements (mini-PRD):
 *   ✔︎ attachMidiGlue(): registers THE action handler + starts the LED
 *     feedback effect; returns a teardown fn.
 *   ✔︎ Deck commands (play/cue/hot-cue/loop) via REBASE ADAPTERs; button
 *     actions fire on press only, releases are ignored by design.
 *   ✔︎ Action on an empty deck -> pushToast, no engine throw.
 *     [if] play pressed on an empty deck [then] toast 'Deck N is empty',
 *       engine untouched
 *     [if] hot-cue pad pressed for a slot the track does not have [then]
 *       toast, no cueJump ⛔️
 *   ✔︎ Continuous controls -> engine setters direct (setTrim/setEq/
 *     setFader/setCrossfader/setMaster, setPitch).
 *     [if] a mixer_channel eq action arrives without band [then ⛔️] throw
 *     [if] pitch fader value 1.0 arrives with range 16 [then] setPitch
 *       receives 1.16 exactly (never out-of-range throw from rounding)
 *   ✔︎ Browse encoder/load delegate to a registered BrowseAdapter (the
 *     browser-panel unit owns selection state); absence is logged loudly.
 *   ✔︎ LED feedback: LedRule triggers evaluated reactively against
 *     deckStates -> sendLed queue.
 *     [if] deck 1 starts playing and a deck_playing LedRule exists [then]
 *       a Note On with velocityOn is queued for that device
 */

import { pushToast } from '$lib/stores.svelte';
import { deckStates, engine, mixerState, pitchRanges } from '$lib/rb/audio-engine.svelte';
import { onMidiFaderMove } from '$lib/rb/fader-ghost.svelte';
import { dispatchPerformanceCommand } from '$lib/rb/performance-ipc.svelte';
import {
	getDeviceMap,
	midiState,
	registerActionHandler,
	sendLed,
	unregisterActionHandler
} from '$lib/rb/midi/webmidi.svelte';
import type { LedTrigger, MidiAction, MidiInputValue } from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';

// ------------------------------------------------------- browse delegation

/** The browser-panel unit registers this so hardware browse controls drive
 * ITS selection state (selection is not engine state). */
export interface BrowseAdapter {
	/** Move the highlighted row by delta (encoder ticks, signed). */
	moveSelection(delta: number): void;
	/** Load the highlighted track onto a deck (LOAD button). */
	loadSelected(deck: DeckId): void;
}

let _browseAdapter: BrowseAdapter | null = null;

export function registerBrowseAdapter(adapter: BrowseAdapter): void {
	if (_browseAdapter !== null) {
		throw new Error('registerBrowseAdapter: an adapter is already registered');
	}
	_browseAdapter = adapter;
}

function _requireBrowseAdapter(what: string): BrowseAdapter | null {
	if (_browseAdapter === null) {
		// Loud, not fatal: hardware works before the browser panel mounts.
		console.error(`[midi-glue] ${what} arrived but no BrowseAdapter is registered`);
		pushToast('Browse control ignored - track browser not ready', 'error');
		return null;
	}
	return _browseAdapter;
}

// ---------------------------------------------------------------- _helpers

function _deckIsEmpty(deck: DeckId): boolean {
	return deckStates[deck].stable_id === null;
}

function _toastEmptyDeck(deck: DeckId, what: string): void {
	pushToast(`Deck ${deck} is empty - load a track before ${what}`, 'error');
}

function _pressed(value: MidiInputValue): boolean {
	if (value.kind !== 'button') {
		throw new Error(`button action received non-button input ${value.kind}`);
	}
	return value.pressed;
}

function _continuous01(value: MidiInputValue): number {
	if (value.kind !== 'continuous' && value.kind !== 'continuous14') {
		throw new Error(`continuous action received non-continuous input ${value.kind}`);
	}
	return value.value01;
}

/** Glue-local overlay while MASTER CUE is engaged (no IPC command). */
let _masterCue: { savedMix: number } | null = null;

/** Test-only reset for master_cue latch/hold state. */
export function _resetMasterCueForTests(): void {
	_masterCue = null;
}

function _engageMasterCue(): void {
	if (_masterCue !== null) return;
	_masterCue = { savedMix: mixerState.headphones.mix };
	void dispatchPerformanceCommand({ type: 'headphone_mix', value: 1 });
}

function _disengageMasterCue(): void {
	if (_masterCue === null) return;
	const savedMix = _masterCue.savedMix;
	_masterCue = null;
	void dispatchPerformanceCommand({ type: 'headphone_mix', value: savedMix });
}

function _dispatchHeadphoneMix(value: number): void {
	if (_masterCue !== null) {
		_masterCue.savedMix = value;
		return;
	}
	void dispatchPerformanceCommand({ type: 'headphone_mix', value });
}

// ----------------------------------------------------------- REBASE ADAPTERS
// Deck COMMANDS. Each function is the single call site to swap to the
// typed performance-ipc command path when Codex's abstraction lands.
// Keep each body a one-liner-per-branch so the rebase diff stays 5 lines.

/** REBASE ADAPTER: play/pause toggle command for one deck. */
function _cmdPlayToggle(deck: DeckId, pressT0Ms?: number): void {
	void dispatchPerformanceCommand(
		{ type: 'play', deck, playing: !deckStates[deck].playing },
		pressT0Ms
	);
}

/** REBASE ADAPTER: physical CUE button command for one deck. */
function _cmdPressCue(deck: DeckId, pressT0Ms?: number): void {
	void dispatchPerformanceCommand({ type: 'cue', deck }, pressT0Ms);
}

/** REBASE ADAPTER: hot-cue pad command (jump to slot's in point). Slot-
 * addressed, not a raw ms (#884): hot_cue_trigger, unlike a plain seek, can
 * honour BeatSyncMax and arm for the deck's own next downbeat. pressT0Ms is
 * the MIDI receipt stamp, same contract as _cmdPlayToggle/_cmdPressCue. */
function _cmdHotCue(deck: DeckId, slot: HotCueSlot, pressT0Ms?: number): void {
	void dispatchPerformanceCommand({ type: 'hot_cue_trigger', deck, slot }, pressT0Ms);
}

/** REBASE ADAPTER: engage an auto/beat loop from the current position. */
function _cmdBeatLoop(deck: DeckId, beats: number): void {
	void dispatchPerformanceCommand({ type: 'beat_loop', deck, beats });
}

/** REBASE ADAPTER: disengage the active loop. */
function _cmdLoopExit(deck: DeckId): void {
	void dispatchPerformanceCommand({ type: 'loop', deck, loop: null });
}

// ------------------------------------------------------------ action switch

/** The action switch. Exported for unit tests; production wiring goes
 * through attachMidiGlue() -> registerActionHandler. */
export function handleMidiAction(
	action: MidiAction,
	value: MidiInputValue,
	_deviceId?: string,
	pressT0Ms?: number
): void {
	switch (action.type) {
		case 'deck_play_toggle': {
			if (!_pressed(value)) return;
			if (_deckIsEmpty(action.deck)) return _toastEmptyDeck(action.deck, 'play');
			_cmdPlayToggle(action.deck, pressT0Ms);
			return;
		}
		case 'deck_cue': {
			if (!_pressed(value)) return;
			if (_deckIsEmpty(action.deck)) return _toastEmptyDeck(action.deck, 'cueing');
			_cmdPressCue(action.deck, pressT0Ms);
			return;
		}
		case 'deck_hot_cue': {
			if (!_pressed(value)) return;
			if (_deckIsEmpty(action.deck)) return _toastEmptyDeck(action.deck, 'hot cues');
			const cue = _findHotCue(action.deck, action.slot);
			if (cue === null) {
				// Empty slot is a REAL library state, not an error to throw on.
				pushToast(`Deck ${action.deck}: no hot cue in slot ${action.slot}`, 'info');
				return;
			}
			_cmdHotCue(action.deck, action.slot, pressT0Ms);
			return;
		}
		case 'deck_beat_loop': {
			if (!_pressed(value)) return;
			if (_deckIsEmpty(action.deck)) return _toastEmptyDeck(action.deck, 'looping');
			_cmdBeatLoop(action.deck, action.beats);
			return;
		}
		case 'deck_loop_exit': {
			if (!_pressed(value)) return;
			if (_deckIsEmpty(action.deck)) return _toastEmptyDeck(action.deck, 'looping');
			_cmdLoopExit(action.deck);
			return;
		}
		case 'mixer_channel': {
			const v = _continuous01(value);
			if (action.target === 'trim') {
				void dispatchPerformanceCommand({ type: 'trim', deck: action.deck, value: v });
			} else if (action.target === 'eq') {
				if (action.band === undefined) {
					throw new Error('mixer_channel eq action requires band (device map bug)');
				}
				void dispatchPerformanceCommand(
					{ type: 'eq', deck: action.deck, band: action.band, value: v },
					pressT0Ms
				);
			} else if (action.target === 'fader') {
				onMidiFaderMove(action.deck, v, mixerState.channels[action.deck].fader);
				void dispatchPerformanceCommand({ type: 'fader', deck: action.deck, value: v }, pressT0Ms);
			} else if (action.target === 'filter') {
				void dispatchPerformanceCommand({ type: 'filter', deck: action.deck, value: v }, pressT0Ms);
			} else {
				const _exhaustive: never = action.target;
				throw new Error(`Unhandled mixer_channel target: ${_exhaustive}`);
			}
			return;
		}
		case 'mixer_global': {
			const v = _continuous01(value);
			if (action.target === 'crossfader') {
				void dispatchPerformanceCommand({ type: 'crossfader', value: v }, pressT0Ms);
			} else if (action.target === 'master') {
				void dispatchPerformanceCommand({ type: 'master_volume', value: v });
			} else {
				const _exhaustive: never = action.target;
				throw new Error(`Unhandled mixer_global target: ${_exhaustive}`);
			}
			return;
		}
		case 'channel_cue': {
			if (!_pressed(value)) return;
			void dispatchPerformanceCommand({
				type: 'channel_cue',
				deck: action.deck,
				enabled: !mixerState.channels[action.deck].cue_enabled
			});
			return;
		}
		case 'headphone_mix': {
			_dispatchHeadphoneMix(_continuous01(value));
			return;
		}
		case 'headphone_level': {
			void dispatchPerformanceCommand({
				type: 'headphone_level',
				value: _continuous01(value)
			});
			return;
		}
		case 'master_cue': {
			if (action.mode === 'latch') {
				if (!_pressed(value)) return;
				if (_masterCue === null) _engageMasterCue();
				else _disengageMasterCue();
			} else if (action.mode === 'hold') {
				if (_pressed(value)) _engageMasterCue();
				else _disengageMasterCue();
			} else {
				const _exhaustive: never = action.mode;
				throw new Error(`Unhandled master_cue mode: ${_exhaustive}`);
			}
			return;
		}
		case 'deck_pitch': {
			const v = _continuous01(value);
			if (_deckIsEmpty(action.deck)) return _toastEmptyDeck(action.deck, 'pitching');
			void dispatchPerformanceCommand({
				type: 'tempo', deck: action.deck, ratio: pitchRatioFromFader(v, pitchRanges[action.deck])
			});
			return;
		}
		case 'browse_encoder': {
			if (value.kind !== 'relative') {
				throw new Error(`browse_encoder requires a relative binding, got ${value.kind}`);
			}
			_requireBrowseAdapter('browse_encoder')?.moveSelection(value.delta);
			return;
		}
		case 'browse_load': {
			if (!_pressed(value)) return;
			_requireBrowseAdapter('browse_load')?.loadSelected(action.deck);
			return;
		}
		case 'shift_modifier': {
			// Consumed inside webmidi.svelte.ts; reaching the glue is a bug.
			throw new Error('shift_modifier must never reach the action glue');
		}
		default: {
			const _exhaustive: never = action;
			throw new Error(`Unhandled MidiAction: ${JSON.stringify(_exhaustive)}`);
		}
	}
}

/** Hot-cue in-point for a slot, or null when the slot is empty. */
function _findHotCue(deck: DeckId, slot: HotCueSlot): number | null {
	const cue = deckStates[deck].hot_cues.find((c) => c.slot === slot);
	return cue === undefined ? null : cue.in_ms;
}

/** Fader position 0..1 -> playbackRate ratio within +-range%.
 * Orientation: 0 -> -range, 1 -> +range; hardware that runs the other way
 * sets invert on its binding (device-map concern, not glue). Exported for
 * unit tests. */
export function pitchRatioFromFader(value01: number, rangePct: number): number {
	if (!Number.isFinite(value01) || value01 < 0 || value01 > 1) {
		throw new RangeError(`pitchRatioFromFader: value01 must be 0..1, got ${value01}`);
	}
	return 1 + (value01 * 2 - 1) * (rangePct / 100);
}

// ------------------------------------------------------------- led feedback

/** True when a LedRule's watched state is currently active. Reads ONLY
 * reactive stores so the caller's $effect re-runs on change. */
export function ledTriggerActive(trigger: LedTrigger): boolean {
	if (trigger.kind === 'deck_playing') {
		return deckStates[trigger.deck].playing;
	} else if (trigger.kind === 'deck_loaded') {
		return deckStates[trigger.deck].stable_id !== null;
	} else if (trigger.kind === 'loop_engaged') {
		const loop = deckStates[trigger.deck].loop;
		return loop !== null && loop.engaged;
	} else if (trigger.kind === 'hot_cue_present') {
		return deckStates[trigger.deck].hot_cues.some((c) => c.slot === trigger.slot);
	} else if (trigger.kind === 'channel_cue_enabled') {
		return mixerState.channels[trigger.deck].cue_enabled;
	}
	const _exhaustive: never = trigger;
	throw new Error(`Unhandled LedTrigger: ${JSON.stringify(_exhaustive)}`);
}

function _syncLeds(): void {
	for (const device of midiState.devices) {
		const map = getDeviceMap(device.id);
		if (map === null || map.leds === undefined || map.leds.length === 0) continue;
		if (!device.hasOutput) {
			// Map declares LEDs but the port has no output side: loud, once per sync.
			console.error(`[midi-glue] ${device.name}: LedRules declared but device has no MIDI output`);
			continue;
		}
		for (const rule of map.leds) {
			const on = ledTriggerActive(rule.trigger);
			sendLed(device.id, rule.out.ch, rule.out.note, on ? rule.out.velocityOn : rule.out.velocityOff);
		}
	}
}

// ---------------------------------------------------------------- lifecycle

let _attached = false;

/** Wire the whole glue layer: register the action handler and start the
 * reactive LED-feedback effect. Call ONCE from the /performance page mount
 * (after registerDeviceMap calls, before/after initMidi both fine).
 * Returns a teardown that stops the LED effect. */
export function attachMidiGlue(): () => void {
	if (_attached) throw new Error('attachMidiGlue: already attached');
	_attached = true;
	registerActionHandler(handleMidiAction);
	const stopLeds = $effect.root(() => {
		$effect(() => {
			_syncLeds();
		});
	});
	return () => {
		stopLeds();
		// Release the handler webmidi holds, not just this module's latch.
		// registerActionHandler() throws while one is registered, so leaving it
		// behind made the next attach (a /performance remount) throw from
		// inside requestMidiAccess(), whose catch also calls
		// setMidiEnabledChoice(false) -- losing the user's MIDI opt-in on the
		// way out. unregisterActionHandler()'s docstring already said the
		// teardown calls it; only the call was missing.
		unregisterActionHandler();
		_attached = false;
	};
}
