/**
 * Controller-local performance-pad state and actions.
 *
 * This state describes what a stateful hardware pad surface currently means;
 * it never substitutes for engine state. Each WebMIDI input gets independent
 * modes and manual-loop gesture state so two attached controllers cannot
 * change one another's pad surface.
 */

import { deckStates, mixerState } from '$lib/rb/audio-engine.svelte';
import type { DeckId } from '$lib/rb/deck-id';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';
import type { ControllerPadMode } from '$lib/rb/midi/midi-types';
import { dispatchPerformanceCommand } from '$lib/rb/performance-ipc.svelte';
import { stemDialAssignment, type EqDial } from '$lib/rb/stem-dial-map';
import { hotCueEditsAllowed } from '$lib/rb/track-source';
import { CONTROLLER_LOOP_BEATS } from '$lib/rb/midi/controller-loop-pads';
import { CONTROLLER_NEURAL_STEMS, controllerStemPadsAvailable } from '$lib/rb/midi/controller-stem-pads';

const PAD_DEFAULT_MODE: ControllerPadMode = 'hot_cue';

// The action adapter owns user notifications; pad state does not depend on
// the application's global UI store.
type Notify = (message: string, tone: 'info' | 'warn' | 'error') => void;

export function toggleControllerStemEq(deck: DeckId, notify: Notify): void {
	const enabled = !mixerState.channels[deck].stem_eq_mode;
	const stems = deckStates[deck].stems;
	if (enabled && stems.status !== 'ready') {
		notify(`Deck ${deck}: stem EQ unavailable (${stems.status})`, 'warn');
		return;
	}
	if (enabled && !stems.available_controls.includes('drums')) {
		notify(`Deck ${deck}: no separate drums stem; LOW remains standard EQ`, 'warn');
	}
	void dispatchPerformanceCommand({ type: 'stem_eq_mode', deck, enabled });
}

/** MIDI and the visible mixer share the same fixed HI/MID/LOW stem slots.
 * A missing slot remains EQ, exactly as ChannelStrip renders it. */
export function setControllerEq(
	deck: DeckId, band: EqDial, value: number, notify: Notify, pressT0Ms?: number
): void {
	if (mixerState.channels[deck].stem_eq_mode) {
		const stems = deckStates[deck].stems;
		if (stems.status !== 'ready') {
			notify(`Deck ${deck}: stem EQ unavailable (${stems.status}); press N to return to EQ`, 'warn');
			return;
		}
		const stem = stemDialAssignment(stems.available_controls)[band];
		if (stem !== null) {
			void dispatchPerformanceCommand({ type: 'stem_gain', deck, stem, value }, pressT0Ms);
			return;
		}
	}
	void dispatchPerformanceCommand({ type: 'eq', deck, band, value }, pressT0Ms);
}

const _padModes = new Map<string, Record<DeckId, ControllerPadMode>>();
// Supersedes the position-only manual gesture: a reload must invalidate IN,
// even when the same track is loaded again. Auto length is a deck preference.
type LoopGesture = { generation: number; inMs: number | null; autoBeats: number };
const _loopGestures = new Map<string, Partial<Record<DeckId, LoopGesture>>>();
// The most recent held pad owns each deck's momentary loop. An older pad or
// another controller cannot release it. SHIFT changes the wire bank, not the
// physical pad identity, so a modifier change during a hold still releases.
type BounceHold = { device: string; pad: number; generation: number };
const _bounceHolds = new Map<DeckId, BounceHold>();

function _releaseBounce(deck: DeckId, deviceId: string | undefined, pad?: number): void {
	const hold = _bounceHolds.get(deck);
	if (hold === undefined || hold.device !== _controllerKey(deviceId) ||
		(pad !== undefined && hold.pad !== pad)) return;
	_bounceHolds.delete(deck);
	if (deckStates[deck].stable_id !== null && deckStates[deck].load_generation === hold.generation) {
		void dispatchPerformanceCommand({ type: 'loop', deck, loop: null, if_load_generation: hold.generation });
	}
}

/** Called only for an observed disconnected device, never inferred from a
 * missing test input. Reconnection starts with a fresh controller surface. */
export function releaseControllerDevice(deviceId: string): void {
	for (const deck of _bounceHolds.keys()) _releaseBounce(deck, deviceId);
	_padModes.delete(deviceId);
	_loopGestures.delete(deviceId);
}

function _loopGesture(deviceId: string | undefined, deck: DeckId): LoopGesture {
	const key = _controllerKey(deviceId);
	let gestures = _loopGestures.get(key);
	if (gestures === undefined) {
		gestures = {};
		_loopGestures.set(key, gestures);
	}
	const generation = deckStates[deck].load_generation;
	let gesture = gestures[deck];
	if (gesture === undefined || gesture.generation !== generation) {
		gesture = { generation, inMs: null, autoBeats: gesture?.autoBeats ?? 4 };
		gestures[deck] = gesture;
	}
	return gesture;
}

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

export function resetControllerPadRuntime(): void {
	// Route unmount owns engine disposal; do not enqueue cleanup commands into
	// the command session it is about to invalidate.
	_bounceHolds.clear();
	_padModes.clear();
	_loopGestures.clear();
}

function _deckIsEmpty(deck: DeckId): boolean {
	return deckStates[deck].stable_id === null;
}

function _toastEmptyDeck(deck: DeckId, notify: Notify): void {
	notify(`Deck ${deck} is empty - load a track before using performance pads`, 'error');
}

function _slotForPad(pad: 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8): HotCueSlot {
	return String.fromCharCode('A'.charCodeAt(0) + pad - 1) as HotCueSlot;
}

function _findHotCue(deck: DeckId, slot: HotCueSlot): number | null {
	const cue = deckStates[deck].hot_cues.find((candidate) => candidate.slot === slot);
	return cue === undefined ? null : cue.in_ms;
}

export function persistentCuePositionMs(positionMs: number): number {
	if (!Number.isFinite(positionMs) || positionMs < 0) {
		throw new RangeError('presented hot-cue position must be finite and non-negative');
	}
	return Math.round(positionMs);
}

function _cmdHotCue(deck: DeckId, slot: HotCueSlot, pressT0Ms?: number): void {
	void dispatchPerformanceCommand({ type: 'hot_cue_trigger', deck, slot }, pressT0Ms);
}

function _cmdBeatLoop(deck: DeckId, beats: number): void {
	void dispatchPerformanceCommand({ type: 'beat_loop', deck, beats });
}

function _cmdLoopExit(deck: DeckId): void {
	void dispatchPerformanceCommand({ type: 'loop', deck, loop: null });
}

export function cycleControllerManualLoop(
	deck: DeckId,
	deviceId: string | undefined,
	notify: Notify
): void {
	const gesture = _loopGesture(deviceId, deck);
	if (deckStates[deck].loop?.engaged === true) {
		gesture.inMs = null;
		_cmdLoopExit(deck);
		return;
	}
	const loopInMs = gesture.inMs;
	if (loopInMs === null) {
		gesture.inMs = deckStates[deck].position_ms;
		notify(`Deck ${deck}: loop in set`, 'info');
		return;
	}
	if (!Number.isFinite(loopInMs)) {
		gesture.inMs = null;
		_cmdLoopExit(deck);
		return;
	}
	const loopOutMs = deckStates[deck].position_ms;
	if (loopOutMs <= loopInMs) {
		notify(`Deck ${deck}: loop out must follow loop in`, 'error');
		return;
	}
	// Retain stage 2 until the next press even if the revisioned engine command
	// has not presented yet; rapid IN/OUT/EXIT cannot accidentally start a new
	// loop-in gesture while LOOP OUT is still queued.
	gesture.inMs = Number.NaN;
	void dispatchPerformanceCommand({ type: 'loop', deck, loop: { in_ms: loopInMs, out_ms: loopOutMs } });
}

/** Factory SHIFT+LOOP cancels pending IN, otherwise toggles Auto Loop. */
export function toggleControllerAutoLoop(
	deck: DeckId, deviceId: string | undefined, notify: Notify
): void {
	const gesture = _loopGesture(deviceId, deck);
	if (gesture.inMs !== null && Number.isFinite(gesture.inMs)) {
		gesture.inMs = null;
		notify(`Deck ${deck}: loop in cancelled`, 'info');
		return;
	}
	gesture.inMs = null;
	const loop = deckStates[deck].loop;
	if (loop?.engaged) {
		if (loop.beat_length !== null) gesture.autoBeats = loop.beat_length;
		_cmdLoopExit(deck);
	} else {
		_cmdBeatLoop(deck, gesture.autoBeats);
	}
}

export function selectControllerPadMode(
	deviceId: string | undefined,
	deck: DeckId,
	mode: ControllerPadMode,
	onModeChanged: () => void,
	notify: Notify
): void {
	if (controllerPadMode(deviceId, deck) !== mode) _releaseBounce(deck, deviceId);
	_padModesFor(deviceId)[deck] = mode;
	onModeChanged();
	if (mode === 'neural_mix' && !controllerStemPadsAvailable(deckStates[deck].stems)) {
		notify(`Deck ${deck}: Neural Mix pads require four ready stems (${deckStates[deck].stems.status})`, 'warn');
	} else if (!['hot_cue', 'auto_loop', 'bounce_loop', 'neural_mix'].includes(mode)) {
		notify(`Deck ${deck}: ${mode.replaceAll('_', ' ')} pads are not available yet`, 'warn');
	}
}

export function runControllerPad(
	deviceId: string | undefined,
	deck: DeckId,
	pad: 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8,
	shifted: boolean,
	pressed: boolean,
	notify: Notify,
	pressT0Ms?: number
): void {
	// A release belongs to its original gesture, even after mode/SHIFT changes.
	if (!pressed) {
		_releaseBounce(deck, deviceId, pad);
		return;
	}
	const mode = controllerPadMode(deviceId, deck);
	if (_deckIsEmpty(deck)) {
		if (pressed) _toastEmptyDeck(deck, notify);
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
		// The one predicate HotCueBank's pads and the hot_cue_save IPC gate
		// share: a stick track keeps its cues in the session, so hardware
		// must not refuse what the visible pads allow.
		if (!hotCueEditsAllowed(deckStates[deck].stable_id, deckStates[deck].has_rb_mapping)) {
			notify(`Deck ${deck}: this track cannot persist Rekordbox hot cues`, 'error');
			return;
		}
		void dispatchPerformanceCommand({
			type: 'hot_cue_save',
			deck,
			slot,
			// Presented transport is sub-millisecond; the persistent Rekordbox
			// cue contract is integer milliseconds. Round at this boundary so a
			// physical pad can save the position the engine actually publishes.
			in_ms: persistentCuePositionMs(deckStates[deck].position_ms),
			revision: deckStates[deck].hot_cue_revisions[slot]
		});
		return;
	}
	if (mode === 'auto_loop') {
		if (!pressed) return;
		const beats = CONTROLLER_LOOP_BEATS.auto_loop[pad - 1];
		const gesture = _loopGesture(deviceId, deck);
		gesture.inMs = null;
		gesture.autoBeats = beats;
		const loop = deckStates[deck].loop;
		// The engine's beat_loop command deliberately restarts a matching loop.
		// Auto Loop pads instead toggle that length off on a second press.
		if (loop?.engaged && loop.beat_length === beats) _cmdLoopExit(deck);
		else _cmdBeatLoop(deck, beats);
		return;
	}
	if (mode === 'bounce_loop') {
		const generation = deckStates[deck].load_generation;
		_bounceHolds.set(deck, { device: _controllerKey(deviceId), pad, generation });
		void dispatchPerformanceCommand({ type: 'beat_loop', deck,
			beats: CONTROLLER_LOOP_BEATS.bounce_loop[pad - 1], if_load_generation: generation });
		return;
	}
	if (mode === 'neural_mix') {
		const stems = deckStates[deck].stems;
		if (!controllerStemPadsAvailable(stems)) {
			notify(`Deck ${deck}: Neural Mix pads require four ready stems (${stems.status})`, 'warn');
			return;
		}
		const stem = CONTROLLER_NEURAL_STEMS[(pad - 1) % 4];
		const control = stems.controls[stem];
		if (pad <= 4) void dispatchPerformanceCommand({ type: 'stem_mute', deck, stem, muted: !control.muted });
		else void dispatchPerformanceCommand({ type: 'stem_solo', deck, stem, solo: !control.solo, exclusive: true });
		return;
	}
	// Unsupported modes intentionally remain inert after the warning emitted
	// when selected. Never fall through to a surprising hot-cue action.
}
