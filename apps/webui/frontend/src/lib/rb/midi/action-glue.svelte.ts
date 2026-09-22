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
import {
	deckStates,
	engine,
	mixerState,
	peekDeckMeterReading,
	pitchRanges
} from '$lib/rb/audio-engine.svelte';
import { dispatchPerformanceCommand } from '$lib/rb/performance-ipc.svelte';
import {
	getDeviceMap,
	midiState,
	registerActionHandler,
	sendCc,
	sendLed,
	unregisterActionHandler
} from '$lib/rb/midi/webmidi.svelte';
import type {
	ControllerPadMode,
	LedTrigger,
	MidiAction,
	MidiInputValue
} from '$lib/rb/midi/midi-types';
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

const PAD_DEFAULT_MODE: ControllerPadMode = 'hot_cue';
const AUTO_LOOP_BEATS = [0.25, 0.5, 1, 2, 4, 8, 16, 32] as const;
const BOUNCE_LOOP_BEATS = [0.0625, 0.125, 0.25, 0.5, 1, 2, 4, 8] as const;
const MIDI_METER_INTERVAL_MS = 50;

/** Controller-local state is keyed by WebMIDI input id. It never substitutes
 * for engine state: it only describes which meaning a stateful pad surface
 * currently exposes and a not-yet-complete manual-loop gesture. */
const _padModes = new Map<string, Record<DeckId, ControllerPadMode>>();
const _manualLoopInMs = new Map<string, Partial<Record<DeckId, number>>>();

function _controllerKey(deviceId: string | undefined): string {
	return deviceId ?? '__direct_test__';
}

function _padModesFor(deviceId: string | undefined): Record<DeckId, ControllerPadMode> {
	const key = _controllerKey(deviceId);
	let modes = _padModes.get(key);
	if (modes === undefined) {
		modes = { 1: PAD_DEFAULT_MODE, 2: PAD_DEFAULT_MODE, 3: PAD_DEFAULT_MODE, 4: PAD_DEFAULT_MODE };
		_padModes.set(key, modes);
	}
	return modes;
}

export function controllerPadMode(deviceId: string | undefined, deck: DeckId): ControllerPadMode {
	return _padModesFor(deviceId)[deck];
}

export function _resetControllerStateForTests(): void {
	_padModes.clear();
	_manualLoopInMs.clear();
}

export function registerBrowseAdapter(adapter: BrowseAdapter): () => void {
	if (_browseAdapter !== null) {
		throw new Error('registerBrowseAdapter: an adapter is already registered');
	}
	_browseAdapter = adapter;
	return () => {
		if (_browseAdapter !== adapter) {
			throw new Error('registerBrowseAdapter: adapter ownership changed before cleanup');
		}
		_browseAdapter = null;
	};
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

function _slotForPad(pad: 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8): HotCueSlot {
	return String.fromCharCode('A'.charCodeAt(0) + pad - 1) as HotCueSlot;
}

function _manualLoopCycle(deck: DeckId, deviceId: string | undefined): void {
	const key = _controllerKey(deviceId);
	let pending = _manualLoopInMs.get(key);
	if (pending === undefined) {
		pending = {};
		_manualLoopInMs.set(key, pending);
	}
	if (deckStates[deck].loop?.engaged === true) {
		delete pending[deck];
		_cmdLoopExit(deck);
		return;
	}
	const loopInMs = pending[deck];
	if (loopInMs === undefined) {
		pending[deck] = deckStates[deck].position_ms;
		pushToast(`Deck ${deck}: loop in set`, 'info');
		return;
	}
	if (!Number.isFinite(loopInMs)) {
		delete pending[deck];
		_cmdLoopExit(deck);
		return;
	}
	const loopOutMs = deckStates[deck].position_ms;
	if (loopOutMs <= loopInMs) {
		pushToast(`Deck ${deck}: loop out must follow loop in`, 'error');
		return;
	}
	// Retain stage 2 until the next press even if the revisioned engine command
	// has not presented yet; rapid IN/OUT/EXIT cannot accidentally start a new
	// loop-in gesture while LOOP OUT is still queued.
	pending[deck] = Number.NaN;
	void dispatchPerformanceCommand({ type: 'loop', deck, loop: { in_ms: loopInMs, out_ms: loopOutMs } });
}

function _scaleLoop(deck: DeckId, factor: 0.5 | 2): void {
	const loop = deckStates[deck].loop;
	if (loop === null || !loop.engaged) {
		pushToast(`Deck ${deck}: no active loop to ${factor === 0.5 ? 'halve' : 'double'}`, 'info');
		return;
	}
	const lengthMs = loop.out_ms - loop.in_ms;
	void dispatchPerformanceCommand({
		type: 'loop',
		deck,
		loop: { in_ms: loop.in_ms, out_ms: loop.in_ms + lengthMs * factor }
	});
}

function _tempoNudge(deck: DeckId, direction: -1 | 1): void {
	const bpm = deckStates[deck].bpm;
	if (bpm === null || bpm <= 0) {
		pushToast(`Deck ${deck}: tempo nudge needs a measured BPM`, 'error');
		return;
	}
	const ratio = deckStates[deck].pitch + (direction * 0.1) / bpm;
	const range = pitchRanges[deck] / 100;
	if (Math.abs(ratio - 1) > range + 1e-9) {
		pushToast(`Deck ${deck}: tempo nudge exceeds the selected pitch range`, 'error');
		return;
	}
	void dispatchPerformanceCommand({ type: 'tempo', deck, ratio });
}

function _selectPadMode(
	deviceId: string | undefined,
	deck: DeckId,
	mode: ControllerPadMode
): void {
	_padModesFor(deviceId)[deck] = mode;
	_syncLeds();
	if (!['hot_cue', 'auto_loop', 'bounce_loop'].includes(mode)) {
		pushToast(`Deck ${deck}: ${mode.replaceAll('_', ' ')} pads are not available yet`, 'warn');
	}
}

function _runControllerPad(
	deviceId: string | undefined,
	deck: DeckId,
	pad: 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8,
	shifted: boolean,
	pressed: boolean,
	pressT0Ms?: number
): void {
	const mode = controllerPadMode(deviceId, deck);
	if (_deckIsEmpty(deck)) {
		if (pressed) _toastEmptyDeck(deck, 'using performance pads');
		return;
	}
	if (mode === 'hot_cue') {
		if (!pressed) return;
		const slot = _slotForPad(pad);
		const cue = _findHotCue(deck, slot);
		if (shifted) {
			if (cue === null) return;
			void dispatchPerformanceCommand({
				type: 'hot_cue_clear',
				deck,
				slot,
				revision: deckStates[deck].hot_cue_revisions[slot]
			});
			return;
		}
		if (cue !== null) {
			_cmdHotCue(deck, slot, pressT0Ms);
			return;
		}
		if (!deckStates[deck].has_rb_mapping) {
			pushToast(`Deck ${deck}: this track cannot persist Rekordbox hot cues`, 'error');
			return;
		}
		void dispatchPerformanceCommand({
			type: 'hot_cue_save',
			deck,
			slot,
			in_ms: deckStates[deck].position_ms,
			revision: deckStates[deck].hot_cue_revisions[slot]
		});
		return;
	}
	if (shifted) return;
	if (mode === 'auto_loop') {
		if (pressed) _cmdBeatLoop(deck, AUTO_LOOP_BEATS[pad - 1]);
		return;
	}
	if (mode === 'bounce_loop') {
		if (pressed) _cmdBeatLoop(deck, BOUNCE_LOOP_BEATS[pad - 1]);
		else _cmdLoopExit(deck);
		return;
	}
	// Unsupported modes intentionally remain inert after the warning emitted
	// when selected. Never fall through to a surprising hot-cue action.
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
		case 'deck_sync_toggle': {
			if (!_pressed(value)) return;
			if (_deckIsEmpty(action.deck)) return _toastEmptyDeck(action.deck, 'syncing');
			void dispatchPerformanceCommand({
				type: 'beat_sync',
				deck: action.deck,
				enabled: !deckStates[action.deck].beat_sync_enabled
			});
			return;
		}
		case 'deck_manual_loop_cycle': {
			if (!_pressed(value)) return;
			if (_deckIsEmpty(action.deck)) return _toastEmptyDeck(action.deck, 'looping');
			_manualLoopCycle(action.deck, _deviceId);
			return;
		}
		case 'deck_loop_scale': {
			if (!_pressed(value)) return;
			if (_deckIsEmpty(action.deck)) return _toastEmptyDeck(action.deck, 'resizing a loop');
			_scaleLoop(action.deck, action.factor);
			return;
		}
		case 'deck_key_sync_toggle': {
			if (!_pressed(value)) return;
			if (_deckIsEmpty(action.deck)) return _toastEmptyDeck(action.deck, 'key sync');
			void dispatchPerformanceCommand({
				type: 'key_sync',
				deck: action.deck,
				enabled: !deckStates[action.deck].key_sync_enabled
			});
			return;
		}
		case 'deck_key_nudge': {
			if (!_pressed(value)) return;
			if (_deckIsEmpty(action.deck)) return _toastEmptyDeck(action.deck, 'changing key');
			void dispatchPerformanceCommand({
				type: 'key_nudge',
				deck: action.deck,
				semitones: action.semitones
			});
			return;
		}
		case 'deck_tempo_nudge': {
			if (!_pressed(value)) return;
			if (_deckIsEmpty(action.deck)) return _toastEmptyDeck(action.deck, 'nudging tempo');
			_tempoNudge(action.deck, action.direction);
			return;
		}
		case 'controller_pad_mode': {
			if (!_pressed(value)) return;
			_selectPadMode(_deviceId, action.deck, action.mode);
			return;
		}
		case 'controller_pad': {
			_runControllerPad(
				_deviceId,
				action.deck,
				action.pad,
				action.shifted,
				_pressed(value),
				pressT0Ms
			);
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
export function ledTriggerActive(trigger: LedTrigger, deviceId?: string): boolean {
	if (trigger.kind === 'deck_playing') {
		return deckStates[trigger.deck].playing;
	} else if (trigger.kind === 'deck_loaded') {
		return deckStates[trigger.deck].stable_id !== null;
	} else if (trigger.kind === 'loop_engaged') {
		const loop = deckStates[trigger.deck].loop;
		return loop !== null && loop.engaged;
	} else if (trigger.kind === 'beat_sync_enabled') {
		return deckStates[trigger.deck].beat_sync_enabled;
	} else if (trigger.kind === 'pad_mode_selected') {
		return controllerPadMode(deviceId, trigger.deck) === trigger.mode;
	} else if (trigger.kind === 'hot_cue_present') {
		return (
			(trigger.padMode === undefined || controllerPadMode(deviceId, trigger.deck) === trigger.padMode) &&
			deckStates[trigger.deck].hot_cues.some((c) => c.slot === trigger.slot)
		);
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
			const on = ledTriggerActive(rule.trigger, device.id);
			sendLed(device.id, rule.out.ch, rule.out.note, on ? rule.out.velocityOn : rule.out.velocityOff);
		}
	}
}

function _syncMeters(forceZero = false): void {
	for (const device of midiState.devices) {
		const map = getDeviceMap(device.id);
		if (map === null || map.meters === undefined || map.meters.length === 0) continue;
		if (!device.hasOutput) continue;
		for (const meter of map.meters) {
			const segments = forceZero ? 0 : peekDeckMeterReading(meter.deck).segments;
			const value = midiMeterValue(segments, meter.out.maxValue);
			sendCc(device.id, meter.out.ch, meter.out.cc, value);
		}
	}
}

export function midiMeterValue(segments: number, maxValue: number): number {
	if (!Number.isInteger(segments) || segments < 0 || segments > 10) {
		throw new RangeError(`midiMeterValue: segments must be an integer in 0..10, got ${segments}`);
	}
	if (!Number.isInteger(maxValue) || maxValue < 1 || maxValue > 127) {
		throw new RangeError(`midiMeterValue: maxValue must be an integer in 1..127, got ${maxValue}`);
	}
	return Math.round((segments / 10) * maxValue);
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
	const meterTimer = setInterval(() => _syncMeters(), MIDI_METER_INTERVAL_MS);
	return () => {
		_syncMeters(true);
		clearInterval(meterTimer);
		stopLeds();
		// Release the handler webmidi holds, not just this module's latch.
		// registerActionHandler() throws while one is registered, so leaving it
		// behind made the next attach (a /performance remount) throw from
		// inside requestMidiAccess(), whose catch also calls
		// setMidiEnabledChoice(false) -- losing the user's MIDI opt-in on the
		// way out. unregisterActionHandler()'s docstring already said the
		// teardown calls it; only the call was missing.
		unregisterActionHandler();
		_padModes.clear();
		_manualLoopInMs.clear();
		_attached = false;
	};
}
