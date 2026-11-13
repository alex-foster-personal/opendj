/**
 * D6 (#9373, issue #9717): what a control shows while a commanded move drives it.
 *
 * Pure geometry and timing for the target ghost and progress mark on Knob and VFader. The
 * runner (`commanded-move-runner.svelte.ts`) publishes one `CommandedMovePresentation` per
 * driven control; the controls only read it, so the current indicator and thumb stay the
 * authoritative value and the ghost never takes input.
 *
 * Reduced motion (A11Y-11811): progress advances in whole quarters instead of every frame,
 * so the mark steps once per beat of a 4-beat move rather than sweeping.
 */
import { defineVariantSetting } from '$lib/settings/variant-setting';

export interface CommandedMovePresentation {
	/** Value the move started from, 0..1. */
	readonly from: number;
	/** Value the move lands on, 0..1. */
	readonly target: number;
	/** Elapsed share of the move's time window, 0..1. */
	readonly progress: number;
}

export const REDUCED_MOTION_PROGRESS_STEPS = 4;

export type CommandedGhostStyle = 'ghost-arc' | 'ghost-only';

export const COMMANDED_GHOST_VARIANT = defineVariantSetting({
	id: 'variant_anim_commanded_ghost',
	label: 'Commanded move target ghost',
	group: 'advanced',
	keywords: ['variant', 'dial', 'fader', 'commanded', 'ghost', 'target', 'progress'],
	detail: 'What a dial or fader shows while a commanded move drives it (issue #9717).',
	variants: [
		{
			value: 'ghost-arc',
			label: 'Ghost and progress',
			summary: 'target tick plus a progress ring or bar'
		},
		{ value: 'ghost-only', label: 'Ghost only', summary: 'target tick, no progress mark' }
	],
	defaultValue: 'ghost-arc'
});

function _clamp01(v: number): number {
	return Math.min(1, Math.max(0, v));
}

/** Elapsed share of a move window at `nowMs`, bounded 0..1; quarter steps under reduced motion. */
export function commandedMoveProgress(
	startMs: number,
	endMs: number,
	nowMs: number,
	reducedMotion: boolean
): number {
	if (![startMs, endMs, nowMs].every(Number.isFinite) || endMs < startMs) {
		throw new RangeError(`commanded move progress: bad window ${startMs}..${endMs} at ${nowMs}`);
	}
	const raw = endMs === startMs ? 1 : _clamp01((nowMs - startMs) / (endMs - startMs));
	if (!reducedMotion) return raw;
	return Math.floor(raw * REDUCED_MOTION_PROGRESS_STEPS) / REDUCED_MOTION_PROGRESS_STEPS;
}

/** The presentation a control shows for one move at `nowMs`. */
export function presentCommandedMove(
	plan: { readonly startMs: number; readonly endMs: number; valueAt(tMs: number): number },
	from: number,
	nowMs: number,
	reducedMotion: boolean
): CommandedMovePresentation {
	return {
		from: _clamp01(from),
		target: _clamp01(plan.valueAt(plan.endMs)),
		progress: commandedMoveProgress(plan.startMs, plan.endMs, nowMs, reducedMotion)
	};
}

/** Parse a stored ghost-style variant value; throws on anything unknown. */
export function commandedGhostStyle(value: string): CommandedGhostStyle {
	if (value === 'ghost-arc' || value === 'ghost-only') return value;
	throw new Error(`commanded ghost style ${JSON.stringify(value)} is not ghost-arc|ghost-only`);
}

/** `stroke-dasharray` drawing `progress` of a ring of `circumference` as one visible dash. */
export function progressRingDash(progress: number, circumference: number): string {
	const done = _clamp01(progress) * circumference;
	return `${done.toFixed(2)} ${(circumference - done + 0.01).toFixed(2)}`;
}

// ----- settle on landing (#14036) -----------------------------------------------------------

/** Settle length: a fixed 240 ms, under one beat up to 250 BPM (the plan carries no tempo). */
export const COMMANDED_SETTLE_MS = 240;
/** Overshoot of a knob's indicator at landing, in degrees of rotation. */
export const KNOB_SETTLE_DEG = 4;
/** Overshoot of a fader's thumb at landing, in pixels. */
export const FADER_SETTLE_PX = 3;
/** Keyframe stops (share of the settle) the CSS animation samples the curve at. */
export const COMMANDED_SETTLE_STOPS = [0, 0.25, 0.5, 0.75, 1] as const;

export type CommandedSettleStyle = 'settle' | 'none';
/** The side a settle overshoots to: the move's direction of travel. */
export type CommandedSettleDirection = -1 | 1;

export const COMMANDED_SETTLE_VARIANT = defineVariantSetting({
	id: 'variant_anim_commanded_settle',
	label: 'Commanded move settle',
	group: 'advanced',
	keywords: ['variant', 'dial', 'fader', 'commanded', 'settle', 'overshoot', 'landing'],
	detail: 'How a dial or fader lands when a commanded move completes (issue #14036).',
	variants: [
		{ value: 'settle', label: 'Settle', summary: 'small overshoot that eases back within a beat' },
		{ value: 'none', label: 'None', summary: 'lands with no settle' }
	],
	defaultValue: 'settle'
});

export interface CommandedSettleGate {
	readonly reducedMotion: boolean;
	readonly style: CommandedSettleStyle;
}

/** Parse a stored settle variant value; throws on anything unknown. */
export function commandedSettleStyle(value: string): CommandedSettleStyle {
	if (value === 'settle' || value === 'none') return value;
	throw new Error(`commanded settle style ${JSON.stringify(value)} is not settle|none`);
}

/** Direction of travel from `from` to `landed`; 0 when the move went nowhere (no settle). */
export function commandedSettleDirection(
	from: number,
	landed: number
): CommandedSettleDirection | 0 {
	if (!Number.isFinite(from) || !Number.isFinite(landed)) {
		throw new RangeError(`commanded settle direction: bad values ${from} -> ${landed}`);
	}
	return landed > from ? 1 : landed < from ? -1 : 0;
}

/** Visual overshoot `elapsedMs` after landing: quadratic ease from `amplitude` to 0, 0 when gated off. */
export function commandedSettleOffset(
	elapsedMs: number,
	amplitude: number,
	direction: CommandedSettleDirection,
	gate: CommandedSettleGate
): number {
	if (!Number.isFinite(elapsedMs) || !Number.isFinite(amplitude) || amplitude < 0) {
		throw new RangeError(
			`commanded settle offset: bad input ${elapsedMs} ms, amplitude ${amplitude}`
		);
	}
	if (gate.reducedMotion || gate.style === 'none') return 0;
	if (elapsedMs < 0 || elapsedMs >= COMMANDED_SETTLE_MS) return 0;
	const left = 1 - elapsedMs / COMMANDED_SETTLE_MS;
	return direction * amplitude * left * left;
}

/** Inline CSS vars `--settle-ms` and `--settle-<i>` sampling the curve at each stop; null when no settle runs. */
export function commandedSettleVars(
	amplitude: number,
	direction: CommandedSettleDirection,
	unit: 'deg' | 'px',
	gate: CommandedSettleGate
): string | null {
	if (commandedSettleOffset(0, amplitude, direction, gate) === 0) return null;
	const stops = COMMANDED_SETTLE_STOPS.map((stop, i) => {
		const v = commandedSettleOffset(stop * COMMANDED_SETTLE_MS, amplitude, direction, gate);
		return `--settle-${i}: ${v.toFixed(2)}${unit};`;
	});
	return [`--settle-ms: ${COMMANDED_SETTLE_MS}ms;`, ...stops].join(' ');
}
