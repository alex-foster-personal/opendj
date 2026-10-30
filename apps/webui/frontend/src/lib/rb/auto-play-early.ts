/**
 * AutoPlay's early decision, the pure half (AUTOPLAN-01, AUTOPLAN-03): when it is due, how far ahead a window must be, and
 * the engine calls. The state and the deck work live in auto-play-early.svelte.ts.
 *
 * Requirements:
 *   ✔︎ A decision is due at the soft target, or earlier on a track too short to fit a plan after it.
 *     [if] the master is under the target and the lead still fits [then] not due
 *     [if] the target is later than duration minus the lead [then] due at duration minus the lead, never past the end
 *   ✔︎ A window is only asked for where the page can still reach it.
 *     [if] the master is past the target [then] the earliest window is the position plus the minimum reach
 *     [if] a window is close [then] the wait for the render shrinks so the plan still lands before it
 *   ✔︎ Anything but a usable answer is a reason, never an exception: the set keeps going on the fallback.
 *     [if] the engine refuses, is unreachable or answers nonsense [then] no track, with the reason
 */
import type { TransitionIntent } from '$lib/rb/auto-play-next-plan';

export const DEFAULT_DECIDE_AFTER_S = 180;
/** Allowed range of the soft target. Past this a master rarely gets there, and under it AutoPlay decides on the intro. */
export const DECIDE_AFTER_MIN_S = 30;
export const DECIDE_AFTER_MAX_S = 600;
/** Time from a load order to a deck the planner can read. */
export const CUE_BUDGET_MS = 3_000;
/** How long a decision waits for the pair to render before it plans from the profiles. A prepare takes about 20 s. */
export const PREPARE_BUDGET_MS = 25_000;
/** The nearest a window may be: cueing and a plan with no render. A good window this close is worth more than a measured M. */
export const MIN_REACH_MS = 12_000;
/** The longest common plan: the lead must cover it, because the style is not known until the engine answers. */
export const ASSUMED_PLAN_BARS = 32;
/** Used only to size the lead when a track reports no tempo. */
const ASSUMED_BPM = 120;
export const SHORTLIST_SIZE = 5;
export const DECIDE_URL = '/api/v1/dj/transition/decide';
export const DECIDE_TIMEOUT_MS = 10_000;

export function requiredLeadMs(bpm: number | null): number {
	const tempo = bpm !== null && bpm > 0 ? bpm : ASSUMED_BPM;
	return CUE_BUDGET_MS + PREPARE_BUDGET_MS + (ASSUMED_PLAN_BARS * 4 * 60_000) / tempo;
}

/** Master position at which the decision is due; null when the track's length is unknown, so nothing can be scheduled. */
export function decisionDueMs(durationMs: number | null, targetMs: number, bpm: number | null): number | null {
	if (durationMs === null || !Number.isFinite(durationMs) || durationMs <= 0) return null;
	return Math.max(0, Math.min(targetMs, durationMs - requiredLeadMs(bpm)));
}

export function earliestWindowMs(positionMs: number, targetMs: number): number {
	return Math.max(targetMs, positionMs + MIN_REACH_MS);
}

/** How long a decision may wait for the render: it must still leave time to cue and plan before the window. */
export function prepareWaitMs(leadMs: number): number {
	return Math.max(0, Math.min(PREPARE_BUDGET_MS, leadMs - MIN_REACH_MS));
}

/** Up to `size` distinct picks, best first. `pick` gets the tracks already taken, so it can offer the next best. */
export function buildShortlist(pick: (taken: ReadonlySet<string>) => string | null, size: number): string[] {
	const taken = new Set<string>();
	while (taken.size < size) {
		const next = pick(taken);
		if (next === null || taken.has(next)) break;
		taken.add(next);
	}
	return [...taken];
}

export interface DecidedWindow {
	start_bar: number;
	start_ms: number;
	max_bars: number;
	label: string;
	energy: number | null;
	vocal_free_bars: number | null;
	score: number;
	reason: string;
}

export interface DecideRequest {
	out_deck: number;
	in_deck: number;
	shortlist: readonly string[];
	intent: TransitionIntent;
	recent_styles: readonly string[];
	earliest_ms: number;
	target_ms: number;
}

export type DecideAnswer =
	| { track: string; window: DecidedWindow; style: string; bars: number; reason: string }
	| { track: null; reason: string };

function _record(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function _answerOf(body: unknown): DecideAnswer {
	if (!_record(body) || typeof body.reason !== 'string') return { track: null, reason: 'decide answered with no reason' };
	if (body.track === null) return { track: null, reason: body.reason };
	const window = body.window;
	if (
		typeof body.track !== 'string' ||
		typeof body.style !== 'string' ||
		typeof body.bars !== 'number' ||
		!_record(window) ||
		typeof window.start_ms !== 'number'
	) {
		return { track: null, reason: 'decide answered a track without its window' };
	}
	return { track: body.track, window: window as unknown as DecidedWindow, style: body.style, bars: body.bars, reason: body.reason };
}

/** The window and track, or why not. A failed request is a reason for the fallback, never an exception. */
export async function requestDecision(request: DecideRequest, fetchImpl: typeof fetch = fetch): Promise<DecideAnswer> {
	const signal = AbortSignal.timeout(DECIDE_TIMEOUT_MS);
	try {
		const response = await fetchImpl(DECIDE_URL, {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify(request),
			signal
		});
		if (!response.ok) return { track: null, reason: `decide answered ${response.status}: ${(await response.text()).slice(0, 200)}` };
		return _answerOf(await response.json());
	} catch (error) {
		return { track: null, reason: `decide unreachable: ${error instanceof Error ? error.message : String(error)}` };
	}
}

/** The AutoPlay menu's one line: what AutoPlay will do, or why it will not plan. */
export function decisionLine(
	decision: {
		phase: string;
		track: string | null;
		style: string | null;
		bars: number | null;
		window: { start_bar: number; label: string } | null;
		due_ms: number | null;
		reason: string | null;
	},
	enabled: boolean
): string {
	if (!enabled) return 'Early planning is off: AutoPlay hands off at the end of the track.';
	const { phase, track, style, bars, window, due_ms, reason } = decision;
	const target = track === null ? 'the next track' : track.slice(0, 12);
	const plan = window === null ? '' : ` at bar ${window.start_bar} (${window.label}), ${style} over ${bars} bars`;
	switch (phase) {
		case 'idle':
			if (due_ms === null) return 'Decides once the master is playing.';
			return `Decides at ${Math.round(due_ms / 1000)} s on the master.${reason === null ? '' : ` ${reason}.`}`;
		case 'deciding':
			return 'Choosing the mix-out point and the next track.';
		case 'cueing':
			return `Loading ${target}${plan}.`;
		case 'preparing':
			return `Scoring ${target}${plan}.`;
		case 'scheduled':
			return `Planned: ${target}${plan}.`;
		case 'running':
			return `Mixing into ${target}${plan}.`;
		case 'done':
			return `Mixed into ${target}.`;
		case 'fallback':
			return `No plan, the end-of-track handoff will run: ${reason}.`;
		default:
			throw new Error(`unhandled AutoPlay decision phase: ${phase}`);
	}
}
