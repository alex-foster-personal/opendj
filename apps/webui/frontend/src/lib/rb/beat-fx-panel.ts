/**
 * Beat FX panel logic (issue #9785, BEATFX-14): the DOM-free half of `BeatFxPanel.svelte`.
 * Every control builds a `beat_fx` command (the shape agents send, issue #9783) and the panel
 * only reads the unit back through its view; nothing here writes unit state.
 *
 * Spec: `(private path)` sections 4.3, 4.4.
 * Mimicked: DJM-A9 Beat FX section (manual pp. 47-50): CH SELECT 1 2 3 4 M lit when
 * selected and blinking while applied, BEAT back/forward with the time in ms, ON/OFF blinking
 * when on, QUANTIZE lit, gray with no grid, hidden when off. X-PAD (issue #13978) mimicked
 * from DJM-900NXS2 p. 14: touch is momentary ON at the division under the finger, ON/OFF
 * pressed during the touch latches. The X-PAD sends only plain `beat_fx {beats, enabled}`
 * commands, so an agent reaches every X-PAD state without it; touch state stays in the panel.
 */
import {
	BEAT_FX_BEATS,
	BEAT_FX_EFFECTS,
	BEAT_FX_TARGETS,
	type BeatFxBeats,
	type BeatFxCommand,
	type BeatFxEffect,
	type BeatFxTarget,
	xpadToBeats
} from './beat-fx';
import type { BeatFxUnitView } from './beat-fx-unit';
import { beatFxEffectDef } from './fx/beat-fx-effects';
import { xpadToMs } from './fx/delay-time';
import type { PerformanceUiCommandResult } from './performance-ipc-types';

/** The UI entry of the typed dispatcher (`runPerformanceCommandFromUi`). */
export type BeatFxPanelDispatch = (command: BeatFxCommand) => Promise<PerformanceUiCommandResult>;

export interface BeatFxChannelLamp {
	target: BeatFxTarget;
	label: string;
	lit: boolean;
	blinking: boolean;
}

export type BeatFxQuantizeLamp = 'on' | 'gray' | 'hidden';

/** X-PAD touch state: `wasOn` and `latched` decide whether pointer up turns the effect off. */
export interface BeatFxXpadTouch {
	touching: boolean;
	wasOn: boolean;
	latched: boolean;
	beats: BeatFxBeats | null;
	/** Whole ms under the finger while the unit is in free TIME mode, else null (issue #10001). */
	timeMs: number | null;
}

/** One X-PAD event: the next touch state and the command to send (null sends nothing). */
export interface BeatFxXpadStep {
	touch: BeatFxXpadTouch;
	command: BeatFxCommand | null;
}

export const XPAD_IDLE: BeatFxXpadTouch = Object.freeze({
	touching: false,
	wasOn: false,
	latched: false,
	beats: null,
	timeMs: null
});

//-----------------------------------------------------------------------------
// display

/** CH SELECT lamps 1 2 3 4 M: the selected one lit, blinking while the effect is on. */
export function beatFxChannelLamps(view: BeatFxUnitView): BeatFxChannelLamp[] {
	return BEAT_FX_TARGETS.map((target) => {
		const lit = view.target === target;
		return {
			target,
			label: target === 'master' ? 'M' : target,
			lit,
			blinking: lit && view.enabled
		};
	});
}

/** The division as the beat buttons print it: 1/16, 1/8, 1/4, 1/2, 3/4, 1, 2, 4, 8, 16; empty with no division. */
export function beatFxDivisionLabel(beats: BeatFxBeats | null): string {
	if (beats === null) return '';
	if (beats === 0.75) return '3/4';
	if (beats < 1) return `1/${Math.round(1 / beats)}`;
	return String(beats);
}

/** The division's time at the current tempo, whole ms; `--` with no tempo, empty with no division. */
export function beatFxTimeReadout(view: BeatFxUnitView): string {
	if (view.beats === null) return '';
	return view.beat_ms === null ? '-- ms' : `${Math.round(view.beat_ms)} ms`;
}

/** QUANTIZE lamp: lit when quantizing on a grid, gray with no grid, hidden when off. */
export function beatFxQuantizeLamp(view: BeatFxUnitView): BeatFxQuantizeLamp {
	if (view.quantize_state === 'on') return 'on';
	if (view.quantize_state === 'no_grid') return 'gray';
	if (view.quantize_state === 'off') return 'hidden';
	const exhaustive: never = view.quantize_state;
	throw new Error(`beat fx quantize_state ${String(exhaustive)} is unknown`);
}

/** The effect select rows: every registered effect, unbuilt ones flagged so the select disables them. */
export function beatFxEffectOptions(): { name: BeatFxEffect; built: boolean }[] {
	return BEAT_FX_EFFECTS.map((name) => ({ name, built: beatFxEffectDef(name).builtOnMain }));
}

//-----------------------------------------------------------------------------
// command builders

/** CH SELECT press. */
export function beatFxTargetCommand(target: BeatFxTarget): BeatFxCommand {
	return { type: 'beat_fx', target };
}

/** Effect select change. */
export function beatFxEffectCommand(effect: BeatFxEffect): BeatFxCommand {
	return { type: 'beat_fx', effect };
}

/** BEAT back (-1) or forward (+1) one division; null at either end of the list or with no division. */
export function beatFxBeatStepCommand(
	current: BeatFxBeats | null,
	direction: -1 | 1
): BeatFxCommand | null {
	if (current === null) return null;
	const index = BEAT_FX_BEATS.indexOf(current);
	if (index === -1) throw new RangeError(`beat fx division ${String(current)} is not in the list`);
	const next = BEAT_FX_BEATS[index + direction];
	return next === undefined ? null : { type: 'beat_fx', beats: next };
}

/** LEVEL/DEPTH knob move; the dispatcher refuses anything outside 0..1. */
export function beatFxLevelCommand(level: number): BeatFxCommand {
	return { type: 'beat_fx', level };
}

/** QUANTIZE toggle. */
export function beatFxQuantizeCommand(view: BeatFxUnitView): BeatFxCommand {
	return { type: 'beat_fx', quantize: !view.quantize };
}

/** ON/OFF press: toggles, except during an X-PAD touch, where it latches the effect on. */
export function beatFxOnOffPress(view: BeatFxUnitView, touch: BeatFxXpadTouch): BeatFxXpadStep {
	if (touch.touching)
		return { touch: { ...touch, latched: true }, command: { type: 'beat_fx', enabled: true } };
	return { touch, command: { type: 'beat_fx', enabled: !view.enabled } };
}

//-----------------------------------------------------------------------------
// X-PAD pointer state machine

/** Pointer position across the pad, 0 at the left edge to 1 at the right. */
export function beatFxXpadPosition(clientX: number, left: number, width: number): number {
	if (!(width > 0)) throw new RangeError(`beat fx X-PAD width must be > 0; got ${width}`);
	return Math.min(1, Math.max(0, (clientX - left) / width));
}

/** Pointer down: the effect turns on at the division (or, in free TIME mode, the ms) under the finger. */
export function beatFxXpadDown(view: BeatFxUnitView, x: number): BeatFxXpadStep {
	const held = { touching: true, wasOn: view.enabled, latched: false };
	if (view.timing.mode === 'ms') {
		const timeMs = Math.round(xpadToMs(x));
		return {
			touch: { ...held, beats: null, timeMs },
			command: { type: 'beat_fx', time_ms: timeMs, enabled: true }
		};
	}
	const beats = xpadToBeats(x);
	return {
		touch: { ...held, beats, timeMs: null },
		command: { type: 'beat_fx', beats, enabled: true }
	};
}

/** Pointer move: a new division or whole ms only when the finger crosses into one; nothing when not touching. */
export function beatFxXpadMove(touch: BeatFxXpadTouch, x: number): BeatFxXpadStep {
	if (!touch.touching) return { touch, command: null };
	if (touch.timeMs !== null) {
		const timeMs = Math.round(xpadToMs(x));
		if (timeMs === touch.timeMs) return { touch, command: null };
		return { touch: { ...touch, timeMs }, command: { type: 'beat_fx', time_ms: timeMs } };
	}
	const beats = xpadToBeats(x);
	if (beats === touch.beats) return { touch, command: null };
	return { touch: { ...touch, beats }, command: { type: 'beat_fx', beats } };
}

/** Pointer up: off again, unless the effect was on before the touch or ON/OFF latched it. */
export function beatFxXpadUp(touch: BeatFxXpadTouch): BeatFxXpadStep {
	if (!touch.touching) return { touch, command: null };
	const keepOn = touch.wasOn || touch.latched;
	return { touch: XPAD_IDLE, command: keepOn ? null : { type: 'beat_fx', enabled: false } };
}

//-----------------------------------------------------------------------------
// dispatch

/** Send one panel command; resolves to the refusal reason to show, or null when it ran. */
export async function runBeatFxPanelCommand(
	dispatch: BeatFxPanelDispatch,
	command: BeatFxCommand | null
): Promise<string | null> {
	if (command === null) return null;
	const result = await dispatch(command);
	if (result.ok) return null;
	return result.error instanceof Error ? result.error.message : String(result.error);
}
