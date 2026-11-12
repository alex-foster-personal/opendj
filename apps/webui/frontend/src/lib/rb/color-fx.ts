/**
 * Color FX (MIXUX-02, COLORFX-01..05): the per-channel color knob plays one of
 * several effects, selectable per channel, as on a Pioneer DJM's Sound Color FX
 * (SPACE, DUB ECHO, ..., FILTER: one selector, one knob per channel). This is the
 * pure half: the wire shape of the `color_fx` command, its validation, and the
 * knob -> gain/frequency targets. The Web Audio graph is `color-fx-graph.ts`;
 * live state, the splice and the tempo follow are `color-fx.svelte.ts`.
 *
 * Effects:
 *  - `filter`: the shipped FILTER law (`filterParamsFromKnob`), unchanged.
 *  - `space`:  reverb. Left of center sends a darkening (lowpassed) signal into
 *              it, right of center a brightening (highpassed) one; the further
 *              the turn, the louder the send and the deeper the tone sweep.
 *  - `echo`:   dub echo, a beat-synced delay with darkening feedback, timed to
 *              the deck's own effective tempo. Same left/right tone law. At the
 *              center detent the SEND closes, never the loop, so the repeats
 *              ring out after the knob comes back, as on the hardware.
 *  - `crush`:  bit depth and sample rate reduction (SNDCOL-10655), an AudioWorklet
 *              kernel with its own dry/wet crossfade (`color-fx-crush.ts`).
 *              Left of center crushes harder the further the turn; right of
 *              center a fixed crush through a rising highpass. Here its targets
 *              are only the FILTER bypass.
 *
 * At the detent every send is 0 and the filter is a dry bypass, so the channel
 * plays exactly dry (bit-exact once any earlier tail has decayed).
 */
import { DECK_IDS, FILTER_DEADZONE_FRAC, type DeckId } from '$lib/player/constants';
import { filterParamsFromKnob } from './audio-engine-guards';

export const COLOR_FX_EFFECTS = ['filter', 'space', 'echo', 'crush'] as const;
export type ColorFxEffect = (typeof COLOR_FX_EFFECTS)[number];

/** Short knob captions, as the DJM prints them over the selector. */
export const COLOR_FX_LABELS: Readonly<Record<ColorFxEffect, string>> = Object.freeze({
	filter: 'FILTER',
	space: 'SPACE',
	echo: 'ECHO',
	crush: 'CRUSH'
});

/** Echo divisions in beats: 1/4, 1/2, 3/4 (the dotted-eighth dub classic) and 1. */
export const COLOR_FX_ECHO_BEATS = [0.25, 0.5, 0.75, 1] as const;
export type ColorFxEchoBeats = (typeof COLOR_FX_ECHO_BEATS)[number];

/** Tempo range the echo accepts; outside it the echo is inert, never guessed. */
export const COLOR_FX_MIN_BPM = 40;
export const COLOR_FX_MAX_BPM = 300;

/** Tone sweep end points (Hz) of the send filters, at the detent and at the stop. */
export const DARK_HZ_NEAR = 9000;
export const DARK_HZ_FAR = 700;
export const BRIGHT_HZ_NEAR = 120;
export const BRIGHT_HZ_FAR = 2400;

export interface ColorFxSettings {
	effect: ColorFxEffect;
	echo_beats: ColorFxEchoBeats;
}

export const DEFAULT_COLOR_FX_SETTINGS: Readonly<ColorFxSettings> = Object.freeze({
	effect: 'filter',
	echo_beats: 0.75
});

/** COLORFX-8341: `next` cycles FILTER -> SPACE -> ECHO -> CRUSH -> FILTER (the Cmd-click on the caption). */
export const COLOR_FX_NEXT = 'next';
export type ColorFxChoice = ColorFxEffect | typeof COLOR_FX_NEXT;

/** COLORFX-13: `deck: 'all'` applies one effect to every channel (rekordbox single mode). */
export const COLOR_FX_ALL_DECKS = 'all';
export type ColorFxDeck = DeckId | typeof COLOR_FX_ALL_DECKS;

/** The knob's center detent, where every effect is exactly dry (12 o'clock). */
export const COLOR_FX_CENTER = 0.5;

/** The `color_fx` command. `deck` (1..4 or `all`) plus at least one of the rest; `value` is the
 * color knob (0..1, 0.5 = center), the same number the `filter` command sets. */
export interface ColorFxCommand {
	type: 'color_fx';
	deck: ColorFxDeck;
	effect?: ColorFxChoice;
	echo_beats?: ColorFxEchoBeats;
	value?: number;
}

/** The effect after `effect` in selector order, wrapping round. */
export function nextColorFxEffect(effect: ColorFxEffect): ColorFxEffect {
	return COLOR_FX_EFFECTS[(COLOR_FX_EFFECTS.indexOf(effect) + 1) % COLOR_FX_EFFECTS.length];
}

/**
 * COLORFX-8341: whether a press on the color slot is the cycle gesture: a plain
 * primary-button press with the platform's command modifier (Cmd on a Mac,
 * Ctrl elsewhere; on a Mac Ctrl-click is the context menu, so it is not one).
 */
export function isColorFxCycleGesture(
	event: { button: number; metaKey: boolean; ctrlKey: boolean },
	modifier: 'Meta' | 'Control'
): boolean {
	if (event.button !== 0) return false;
	return modifier === 'Meta' ? event.metaKey : event.ctrlKey;
}

/** A `color_fx` command resolved against the channel it lands on. */
export interface ColorFxSwitchPlan {
	effect: ColorFxEffect;
	/** Where the knob ends up. */
	value: number;
	/** Whether the effect changed (and so the old one was removed). */
	switched: boolean;
}

/**
 * COLORFX-8341, the switch rule. One knob plays one effect: picking another hands
 * the knob to the new effect and the old one is removed (DJM Sound Color FX:
 * pressing a different button selects that one instead; never stacked). On a
 * real switch the knob returns to the center detent unless the command names a
 * `value`, as Mixxx resets the Quick Effect super knob to the preset's stored
 * position, so the new effect enters dry and never jumps in at the depth the old
 * one was left at. Re-picking the current effect, or sending only `echo_beats`,
 * leaves the knob where it is.
 */
export function planColorFxSwitch(current: ColorFxEffect, knob: number, command: ColorFxCommand): ColorFxSwitchPlan {
	const requested = command.effect;
	const effect =
		requested === undefined ? current : requested === COLOR_FX_NEXT ? nextColorFxEffect(current) : requested;
	const switched = effect !== current;
	const value = command.value ?? (switched ? COLOR_FX_CENTER : knob);
	return { effect, value, switched };
}

/** COLORFX-13: refuse an all-decks `color_fx` the wire cannot apply as one knob. */
export function assertColorFxAllCommand(command: ColorFxCommand): void {
	if (command.deck !== COLOR_FX_ALL_DECKS) {
		throw new TypeError(`assertColorFxAllCommand: deck must be ${COLOR_FX_ALL_DECKS}`);
	}
	if (command.value !== undefined) {
		throw new TypeError(
			'color_fx deck all takes no value: each channel has its own knob, never moved together'
		);
	}
	if (command.effect === COLOR_FX_NEXT) {
		throw new TypeError(
			'color_fx deck all refuses effect next: cycling four channels from different effects is ambiguous; name the effect'
		);
	}
	if (command.effect === undefined) {
		throw new TypeError('color_fx deck all needs an effect');
	}
}

const COMMAND_KEYS = new Set(['type', 'deck', 'effect', 'echo_beats', 'value']);
const DECK_OR_ALL = [...DECK_IDS, COLOR_FX_ALL_DECKS] as const;

/** Validate a `color_fx` command off the wire. Throws with the reason, never guesses. */
export function parseColorFxCommand(record: Record<string, unknown>): ColorFxCommand {
	for (const key of Object.keys(record)) {
		if (!COMMAND_KEYS.has(key)) throw new TypeError(`color_fx has unexpected field ${key}`);
	}
	if (record.type !== 'color_fx') throw new TypeError('color_fx command type must be color_fx');
	if (!(DECK_OR_ALL as readonly unknown[]).includes(record.deck)) {
		throw new TypeError(`color_fx deck must be one of ${DECK_OR_ALL.join('|')}; got ${String(record.deck)}`);
	}
	const out: ColorFxCommand = { type: 'color_fx', deck: record.deck as ColorFxDeck };
	if (record.effect !== undefined) {
		if (record.effect !== COLOR_FX_NEXT && !(COLOR_FX_EFFECTS as readonly unknown[]).includes(record.effect)) {
			throw new TypeError(
				`color_fx effect must be one of ${COLOR_FX_EFFECTS.join('|')}|${COLOR_FX_NEXT}; got ${String(record.effect)}`
			);
		}
		out.effect = record.effect as ColorFxChoice;
	}
	if (record.echo_beats !== undefined) {
		if (!(COLOR_FX_ECHO_BEATS as readonly unknown[]).includes(record.echo_beats)) {
			throw new TypeError(
				`color_fx echo_beats must be one of ${COLOR_FX_ECHO_BEATS.join('|')}; got ${String(record.echo_beats)}`
			);
		}
		out.echo_beats = record.echo_beats as ColorFxEchoBeats;
	}
	if (record.value !== undefined) {
		if (typeof record.value !== 'number' || !Number.isFinite(record.value) || record.value < 0 || record.value > 1) {
			throw new TypeError(`color_fx value must be a number in 0..1; got ${String(record.value)}`);
		}
		out.value = record.value;
	}
	if (out.deck === COLOR_FX_ALL_DECKS) assertColorFxAllCommand(out);
	if (Object.keys(out).length === 2) throw new TypeError('color_fx needs at least one of effect, echo_beats, value');
	return out;
}

/** Echo line length for `beats` at `bpm`, or null with no usable tempo. */
export function echoDelaySeconds(beats: number, bpm: number | null): number | null {
	if (bpm === null || !Number.isFinite(bpm) || bpm < COLOR_FX_MIN_BPM || bpm > COLOR_FX_MAX_BPM) return null;
	return (60 / bpm) * beats;
}

type FilterParams = ReturnType<typeof filterParamsFromKnob>;

/** Everything the knob drives, for one effect at one knob position. */
export interface ColorFxTargets {
	filter: FilterParams;
	darkSend: number;
	darkHz: number;
	brightSend: number;
	brightHz: number;
	echoIn: number;
	spaceIn: number;
}

const FILTER_BYPASS: FilterParams = filterParamsFromKnob(0.5);

/** Signed knob travel away from the detent, -1..1, exactly 0 inside the detent (same detent as FILTER). */
export function colorFxTravel(value: number): number {
	const colour = (value - 0.5) * 2;
	if (Math.abs(colour) < FILTER_DEADZONE_FRAC) return 0;
	const amount = (Math.abs(colour) - FILTER_DEADZONE_FRAC) / (1 - FILTER_DEADZONE_FRAC);
	return colour < 0 ? -amount : amount;
}

/** Knob travel away from the detent on 0..1, 0 inside the detent. */
function _travel(value: number): { side: -1 | 0 | 1; amount: number } {
	const travel = colorFxTravel(value);
	if (travel === 0) return { side: 0, amount: 0 };
	return { side: travel < 0 ? -1 : 1, amount: Math.abs(travel) };
}

/** Exponential-in-Hz sweep, linear in octaves (the filter-taper-laws L2 law). */
function _sweep(near: number, far: number, amount: number): number {
	return near * (far / near) ** amount;
}

/**
 * Targets for `effect` at knob `value`. `echoTimed` says whether the echo has a
 * tempo to run at; without one its input stays closed (inert, reported by the
 * live view). Pure, so the mapping is testable without an AudioContext.
 */
export function colorFxTargets(effect: ColorFxEffect, value: number, echoTimed: boolean): ColorFxTargets {
	const { side, amount } = _travel(value);
	const darkHz = _sweep(DARK_HZ_NEAR, DARK_HZ_FAR, side === -1 ? amount : 0);
	const brightHz = _sweep(BRIGHT_HZ_NEAR, BRIGHT_HZ_FAR, side === 1 ? amount : 0);
	if (effect === 'filter') {
		return { filter: filterParamsFromKnob(value), darkSend: 0, darkHz, brightSend: 0, brightHz, echoIn: 0, spaceIn: 0 };
	} else if (effect === 'space' || effect === 'echo') {
		return {
			filter: FILTER_BYPASS,
			darkSend: side === -1 ? amount : 0,
			darkHz,
			brightSend: side === 1 ? amount : 0,
			brightHz,
			echoIn: effect === 'echo' && echoTimed ? 1 : 0,
			spaceIn: effect === 'space' ? 1 : 0
		};
	} else if (effect === 'crush') {
		// CRUSH crossfades its own wet path against filterDry (color-fx-crush.ts);
		// here the FILTER is bypassed and every SPACE / ECHO send stays closed.
		return { filter: FILTER_BYPASS, darkSend: 0, darkHz, brightSend: 0, brightHz, echoIn: 0, spaceIn: 0 };
	} else {
		const _exhaustive: never = effect;
		throw new Error(`Unhandled color FX effect: ${String(_exhaustive)}`);
	}
}
