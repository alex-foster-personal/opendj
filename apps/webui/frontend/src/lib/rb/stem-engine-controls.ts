/**
 * Stem mute/solo/gain apply extracted from audio-engine.svelte.ts (MIXUX-04).
 * Keeps the hotspot file from growing while stem EQ mode lands.
 */

import type { DeckState } from '$lib/rb/deck-state-types';
import type { DeckId } from '$lib/rb/deck-slots';
import type { MixerChannelState } from '$lib/rb/mixer-types';
import { AlignedStemDeckProcessor, STEM_CONTROLS, type StemControls } from '$lib/rb/stem-graph';
import type { StemControl } from '$lib/rb/stem-types';

export interface StemEngineRuntime {
	processor: unknown;
}

export interface StemEngineControlDeps {
	requireLoaded: (deck: DeckId, op: string) => { st: DeckState; rt: StemEngineRuntime };
	getChannel: (deck: DeckId) => MixerChannelState;
	exclusive?: boolean;
	/** AUDFID-02: the ready bundle held while the deck plays the original
	 * file, or null when none is held (the stems are the deck's processor, or
	 * there is no bundle). */
	heldStems: (deck: DeckId) => AlignedStemDeckProcessor | null;
	/** AUDFID-02: the first touch: make the held stems audible (crossfade on a
	 * playing deck, instant swap on a stopped one). Called after the touched
	 * value is already set on them, so the press is never lost. */
	engageHeldStems: (deck: DeckId) => void;
	/** #14376: a manual gain write cancels that control's waiting scheduled step; true when one was waiting. */
	cancelStemStep?: (deck: DeckId, stem: StemControl) => boolean;
}

export function applyStemControl(
	deck: DeckId,
	stem: StemControl,
	field: 'muted' | 'solo' | 'gain' | 'fx_send',
	value: boolean | number,
	deps: StemEngineControlDeps
): void {
	if (deps.exclusive !== undefined && typeof deps.exclusive !== 'boolean') {
		throw new TypeError('exclusive stem solo must be boolean');
	}
	if (!STEM_CONTROLS.includes(stem)) {
		throw new TypeError(`unknown stem control: ${String(stem)}`);
	}
	const op =
		field === 'muted'
			? 'setStemMute'
			: field === 'solo'
				? 'setStemSolo'
				: field === 'fx_send'
					? 'setStemFxSend'
					: 'setStemGain';
	const loaded = _loadedStemDeck(deps.requireLoaded, deck, op);
	if (loaded === null) {
		// Empty deck: same as unavailable stems. CLI / agent / IPC can fire
		// stem_mute while deck 1 has no track; throwing lights the persistent
		// deck-error banner and Sentry (OPEN-DJ-BE-6Z).
		return;
	}
	const { st, rt } = loaded;
	if (st.stems.status === 'ready' && !st.stems.available_controls.includes(stem)) {
		throw new Error(
			`deck ${deck} stem layout ${String(st.stems.layout)} has no ${stem} control; ` +
				`available: ${st.stems.available_controls.join(', ')}`
		);
	}
	if (st.stems.status === 'unavailable') {
		// A settled "this track has no stem bundle" - not a failure the user
		// caused or can fix by retrying. The controls that could reach here
		// are already disabled in the UI; anything else that calls this
		// (agent orders, replayed commands) gets a silent no-op rather than
		// a throw that would light up the deck's red error banner for the
		// normal, permanent state of any track without stems.
		return;
	}
	const held = st.stems.status === 'ready' ? deps.heldStems(deck) : null;
	const target = held ?? rt.processor;
	if (st.stems.status !== 'ready' || !(target instanceof AlignedStemDeckProcessor)) {
		throw new Error(
			`deck ${deck} stems are ${st.stems.status}: ${st.stems.error ?? 'no aligned artifact'}`
		);
	}
	const controls = {
		vocal: { ...st.stems.controls.vocal },
		instrumental: { ...st.stems.controls.instrumental },
		drums: { ...st.stems.controls.drums },
		bass: { ...st.stems.controls.bass },
		other: { ...st.stems.controls.other }
	};
	if (field === 'solo' && deps.exclusive) {
		for (const control of STEM_CONTROLS) controls[control].solo = false;
	}
	if (field === 'fx_send') {
		// STEMPERF-11663: only the send ramps; the part gains get no call.
		if (typeof value !== 'number') {
			throw new TypeError('setStemFxSend: value must be a number');
		}
		controls[stem].fx_send = value;
		st.stems.controls = controls;
		target.setStemFxSend(stem, value);
		if (held !== null) deps.engageHeldStems(deck);
		return;
	}
	if (field === 'gain') {
		if (typeof value !== 'number') {
			throw new TypeError('setStemGain: value must be a number');
		}
		controls[stem].gain = value;
		if (deps.cancelStemStep?.(deck, stem) === true) target.cancelStemGain(stem);
	} else {
		if (typeof value !== 'boolean') {
			throw new TypeError(`setStem${field === 'muted' ? 'Mute' : 'Solo'}: ${field} must be boolean`);
		}
		controls[stem][field] = value;
	}
	st.stems.controls = controls;
	target.setControls(controls);
	_layWaitingSteps(target, controls, field === 'gain' ? stem : null);
	if (held !== null) deps.engageHeldStems(deck);
}

/** #14217: a plan stem_gain step whose part gain waits on the audio clock for its time. */
interface WaitingStemStep {
	stem: StemControl;
	value: number;
	atSec: number;
}

/** #14217: the stem steps still waiting per stem processor, so a DJ move lays them again over what the DJ set. */
const _waitingSteps = new WeakMap<AlignedStemDeckProcessor, WaitingStemStep[]>();

/** #14217: the part gains from now: the DJ's controls, then each waiting step (time order) on top of the ones before. */
function _gainTimeline(
	controls: StemControls,
	steps: readonly WaitingStemStep[]
): { atSec: number; controls: StemControls }[] {
	const timeline = [{ atSec: 0, controls }];
	for (const { stem, value, atSec } of steps) {
		const before = timeline[timeline.length - 1].controls;
		timeline.push({ atSec, controls: { ...before, [stem]: { ...before[stem], gain: value } } });
	}
	return timeline;
}

/**
 * #14217: a DJ move while plan stem steps wait. A move on a step's own gain drops that step (the DJ wins, as a plain
 * fader set cancels a fader step); the other steps are laid again over what the DJ set, so the step that lands changes
 * only its part and never plays back the controls from when it was scheduled.
 */
function _layWaitingSteps(
	target: AlignedStemDeckProcessor,
	controls: StemControls,
	movedGain: StemControl | null
): void {
	const waiting = _waitingSteps.get(target);
	if (waiting === undefined || waiting.length === 0) return;
	const kept = waiting.filter((step) => step.stem !== movedGain);
	_waitingSteps.set(target, kept);
	target.setGainTimeline(_gainTimeline(controls, kept));
}

export function applyStemEqMode(
	deck: DeckId,
	enabled: boolean,
	getChannel: (deck: DeckId) => MixerChannelState
): void {
	if (typeof enabled !== 'boolean') {
		throw new TypeError('setStemEqMode: enabled must be boolean');
	}
	getChannel(deck).stem_eq_mode = enabled;
}

function _loadedStemDeck(
	requireLoaded: StemEngineControlDeps['requireLoaded'],
	deck: DeckId,
	op: string
): { st: DeckState; rt: StemEngineRuntime } | null {
	try {
		return requireLoaded(deck, op);
	} catch (error) {
		if (_isUnloadedDeckError(error, op, deck)) return null;
		throw error;
	}
}

function _isUnloadedDeckError(error: unknown, op: string, deck: DeckId): boolean {
	return error instanceof Error && error.message === `${op}: no track loaded on deck ${deck}`;
}

/**
 * #14043: a plan's stem_gain step on the audio clock: the part's gain starts at `atSec`; the returned callback writes the
 * deck's stem mirror when the step lands (null when the deck has no stems to set, a no-op like applyStemControl).
 */
export function scheduleStemGain(
	deck: DeckId,
	stem: StemControl,
	value: number,
	atSec: number,
	deps: StemEngineControlDeps
): (() => void) | null {
	if (!STEM_CONTROLS.includes(stem)) throw new TypeError(`unknown stem control: ${String(stem)}`);
	const loaded = _loadedStemDeck(deps.requireLoaded, deck, 'setStemGain');
	if (loaded === null || loaded.st.stems.status === 'unavailable') return null;
	const { st, rt } = loaded;
	const held = st.stems.status === 'ready' ? deps.heldStems(deck) : null;
	const target = held ?? rt.processor;
	if (st.stems.status !== 'ready' || !(target instanceof AlignedStemDeckProcessor)) {
		throw new Error(`deck ${deck} stems are ${st.stems.status}: ${st.stems.error ?? 'no aligned artifact'}`);
	}
	if (!st.stems.available_controls.includes(stem)) throw new Error(`deck ${deck} stem layout ${String(st.stems.layout)} has no ${stem} control`);
	// #14217: only the part gains are laid (the DJ's controls now, the step on its time), never a copy of every control.
	const step: WaitingStemStep = { stem, value, atSec };
	const waiting = [...(_waitingSteps.get(target) ?? []), step].sort((a, b) => a.atSec - b.atSec);
	_waitingSteps.set(target, waiting);
	target.setGainTimeline(_gainTimeline(st.stems.controls, waiting));
	if (held !== null) deps.engageHeldStems(deck);
	return () => {
		// #14217: lands only while it still waits (a DJ move on this gain dropped it), and writes only its own gain.
		const still = _waitingSteps.get(target) ?? [];
		if (!still.includes(step)) return;
		_waitingSteps.set(target, still.filter((other) => other !== step));
		st.stems.controls = { ...st.stems.controls, [stem]: { ...st.stems.controls[stem], gain: value } };
	};
}
