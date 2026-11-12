import { proposalConfidenceReadout } from '$lib/autodj/propose-confidence';

/**
 * AutoPlay Next's planner call (AGENT-48): ask the Python chooser for a transition, and decide which path runs.
 *
 * The chooser stays in Python (apps/transition_score/live.py); this file only carries the request and reads the
 * answer. Any answer without an order, and any failure to get one, selects the fixed featurette with the reason, so
 * the readout can say which path ran and why.
 *
 * Requirements:
 *   ✔︎ An order from the planner selects the planner path.
 *     [if] the answer carries an order [then] the path is planner, with the pick's style and M
 *   ✔︎ Everything else selects the featurette and says why.
 *     [if] the planner answers no order, a non-2xx or nothing [then] the path is featurette with that reason
 */

export const PLANNER_URL = '/api/v1/dj/transition/plan';
export const PREPARE_URL = '/api/v1/dj/transition/prepare';
export const PLAYED_URL = '/api/v1/autodj/pair-transition/played';
/** A press never renders (AGENT-52), so past this something is wrong and the featurette is the better answer. */
export const PLANNER_TIMEOUT_MS = 10_000;
/** How many played styles the page remembers: the planner's own window (apps/autodj/choose.py VARIETY_WINDOW). */
export const VARIETY_WINDOW = 10;

export const TRANSITION_INTENTS = [
	'smooth',
	'energy_up',
	'wind_down',
	'showy',
	'vocals_clean'
] as const;
export type TransitionIntent = (typeof TRANSITION_INTENTS)[number];

export interface PlannerRequest {
	out_deck: number;
	in_deck: number;
	intent: TransitionIntent;
	recent_styles: readonly string[];
	/** AGENT-50: leave the outgoing track near this point, ms on its own clock. */
	from_ms?: number;
	/** AGENT-50: the incoming deck plays now; the page stops and re-cues it before the plan runs. */
	take_incoming?: boolean;
}

export interface PlannerPick {
	candidate: string;
	style: string;
	bars: number;
	/** Null when the press planned from the profiles because nothing scored fits that point yet. */
	score: number | null;
	out_start_bar: number;
	rationale: string;
	planner_confidence?: number | null;
	planner_weakest_factor?: string | null;
	planner_confidence_title?: string | null;
}

export interface PlannerAnswer {
	order: { plan: Record<string, unknown> } | null;
	reason: string;
	intent: string;
	pick: PlannerPick | null;
	out_bar_now: number | null;
	considered: number;
	rendered: number;
	propose_ms: number;
	render_ms: number;
	scored: boolean;
	plan_ms: number;
	/** AGENT-49: this is the pair's saved plan, replayed. */
	saved: boolean;
	/** The plan document that runs, reported back when it completes. */
	plan: Record<string, unknown> | null;
}

export type TransitionChoice =
	| {
			path: 'planner';
			plan: Record<string, unknown>;
			pick: PlannerPick;
			reason: string;
			rendered: number;
			saved: boolean;
			document: Record<string, unknown> | null;
	  }
	| { path: 'featurette'; reason: string };

export function selectTransitionPath(answer: PlannerAnswer): TransitionChoice {
	if (answer.order === null || answer.pick === null)
		return { path: 'featurette', reason: answer.reason };
	return {
		path: 'planner',
		plan: answer.order.plan,
		pick: answer.pick,
		reason: answer.reason,
		rendered: answer.rendered,
		saved: answer.saved === true,
		document: answer.plan ?? null
	};
}

async function _refusal(response: Response): Promise<string> {
	const text = await response.text();
	try {
		const body = JSON.parse(text) as { message?: unknown };
		if (typeof body.message === 'string')
			return `planner answered ${response.status}: ${body.message}`;
	} catch {
		// Not an ErrorBody: the status and the text below say what came back.
	}
	return `planner answered ${response.status}: ${text.slice(0, 200)}`;
}

/** The path to run. A failed request is a reason for the featurette, never an exception: the set keeps going. */
export async function requestPlannedTransition(
	request: PlannerRequest,
	signal: AbortSignal,
	fetchImpl: typeof fetch = fetch
): Promise<TransitionChoice> {
	let response: Response;
	try {
		response = await fetchImpl(PLANNER_URL, {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify(request),
			signal
		});
	} catch (error) {
		return {
			path: 'featurette',
			reason: `planner unreachable: ${error instanceof Error ? error.message : String(error)}`
		};
	}
	if (!response.ok) return { path: 'featurette', reason: await _refusal(response) };
	let answer: unknown;
	try {
		answer = await response.json();
	} catch {
		return { path: 'featurette', reason: 'planner answered 200 with a body that is not JSON' };
	}
	const malformed = _malformed(answer);
	if (malformed !== null)
		return { path: 'featurette', reason: `planner answer malformed: ${malformed}` };
	return selectTransitionPath(answer as PlannerAnswer);
}

function _record(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Why a 200 body is not a PlannerAnswer the page can act on, or null. */
function _malformed(answer: unknown): string | null {
	if (!_record(answer)) return 'not an object';
	if (typeof answer.reason !== 'string') return 'no reason';
	if (answer.order === null) return null;
	if (
		!_record(answer.order) ||
		!_record(answer.order.plan) ||
		typeof answer.order.plan.id !== 'string'
	) {
		return 'order carries no plan id';
	}
	const pick = answer.pick;
	if (!_record(pick) || typeof pick.style !== 'string' || typeof pick.bars !== 'number') {
		return 'an order without its pick';
	}
	return null;
}

/** Ask the engine to score this pair ahead of a press (AGENT-52). Best effort: a press plans without it. */
export async function requestPrepare(
	request: Pick<PlannerRequest, 'out_deck' | 'in_deck' | 'intent'>,
	fetchImpl: typeof fetch = fetch
): Promise<void> {
	const response = await fetchImpl(PREPARE_URL, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(request)
	});
	if (!response.ok) throw new Error(await _refusal(response));
	await response.text();
}

export interface PrepareStatus {
	state: 'warming' | 'ready' | 'failed';
	candidates: number;
	/** The track the engine scored, from its mirror: right after a load this can still be the deck's last one. */
	in_track: string | null;
}

/** How far the pair's prepare has got, starting one only if none exists: AutoPlay's early decision waits on it (AUTOPLAN-04). */
export async function requestPrepareStatus(
	request: Pick<PlannerRequest, 'out_deck' | 'in_deck' | 'intent' | 'from_ms'>,
	fetchImpl: typeof fetch = fetch
): Promise<PrepareStatus> {
	const response = await fetchImpl(PREPARE_URL, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ ...request, status_only: true })
	});
	if (!response.ok) throw new Error(await _refusal(response));
	return (await response.json()) as PrepareStatus;
}

/**
 * AGENT-49: a transition completed, so its plan becomes the pair's default. A replayed saved plan sends no plan: the
 * document the page holds sits on these decks at this bar, and sending it back would replace the saved row.
 */
export async function reportPlayed(
	played: {
		out: string;
		in: string;
		saved: boolean;
		plan: Record<string, unknown> | null;
		style: string;
	},
	fetchImpl: typeof fetch = fetch
): Promise<void> {
	const body =
		played.saved || played.plan === null
			? { out: played.out, in: played.in }
			: {
					out: played.out,
					in: played.in,
					plan: played.plan,
					source: 'planner',
					style: played.style
				};
	const response = await fetchImpl(PLAYED_URL, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(body)
	});
	if (!response.ok)
		throw new Error(`played answered ${response.status}: ${(await response.text()).slice(0, 200)}`);
}

/** What the last or current arm did: the readout and the mirror's `auto_play_next` both show this. */
export interface AutoPlayNextRun {
	/** `none`: a transition asked for at a named point had no plan, and the fixed loop does not apply there. */
	path: 'planner' | 'featurette' | 'none';
	reason: string;
	style: string | null;
	bars: number | null;
	score: number | null;
	out_start_bar: number | null;
	rendered: number | null;
	planning_ms: number;
	saved: boolean;
	planner_confidence: number | null;
	planner_weakest_factor: string | null;
	planner_confidence_title: string | null;
}

export function planRunOf(choice: TransitionChoice, planningMs: number): AutoPlayNextRun {
	const ms = Math.round(planningMs);
	if (choice.path === 'featurette') {
		const empty = {
			style: null,
			bars: null,
			score: null,
			out_start_bar: null,
			rendered: null,
			planner_confidence: null,
			planner_weakest_factor: null,
			planner_confidence_title: null
		};
		return { path: 'featurette', reason: choice.reason, planning_ms: ms, saved: false, ...empty };
	}
	const { style, bars, score, out_start_bar } = choice.pick;
	return {
		path: 'planner',
		reason: choice.reason,
		style,
		bars,
		score,
		out_start_bar,
		rendered: choice.rendered,
		planning_ms: ms,
		saved: choice.saved,
		planner_confidence: choice.pick.planner_confidence ?? null,
		planner_weakest_factor: choice.pick.planner_weakest_factor ?? null,
		planner_confidence_title: choice.pick.planner_confidence_title ?? null
	};
}

export function runLabel(run: AutoPlayNextRun): string {
	const confidence = proposalConfidenceReadout(
		run.planner_confidence,
		run.planner_weakest_factor,
		null
	).label;
	const suffix = confidence ? `; ${confidence}` : '';
	if (run.saved) return `saved: ${run.style} ${run.bars}${suffix}`;
	if (run.path === 'planner')
		return `plan: ${run.style} ${run.bars}${run.score === null ? ' (unscored)' : ''}${suffix}`;
	return run.path === 'featurette' ? 'fixed loop' : 'no plan';
}

export function runTitle(run: AutoPlayNextRun): string {
	const took = `${run.planning_ms} ms planning`;
	if (run.path === 'featurette')
		return `Fixed loop transition, not planned: ${run.reason} (${took})`;
	if (run.path === 'none') return `Nothing armed: ${run.reason} (${took})`;
	if (run.saved)
		return `Saved transition for this pair, starting at outgoing bar ${run.out_start_bar}. ${run.reason} (${took}).`;
	const from =
		run.score === null
			? 'from the track analysis, not yet scored'
			: `from ${run.rendered} scored candidates`;
	const conf = proposalConfidenceReadout(
		run.planner_confidence,
		run.planner_weakest_factor,
		parseConfidenceTitle(run.planner_confidence_title)
	);
	const confPart = conf.title ? ` Planner ${conf.label ?? ''}. ${conf.title}.` : '';
	return `Planned transition starting at outgoing bar ${run.out_start_bar}. ${run.reason}. Chose ${from} (${took}). M is the 0..1 transition quality score.${confPart}`;
}

function parseConfidenceTitle(title: string | null): Record<string, number> | null {
	if (!title) return null;
	const factors: Record<string, number> = {};
	for (const part of title.split(';')) {
		const trimmed = part.trim();
		const space = trimmed.lastIndexOf(' ');
		if (space <= 0) continue;
		const key = trimmed.slice(0, space);
		const value = Number(trimmed.slice(space + 1));
		if (Number.isFinite(value)) factors[key] = value;
	}
	return Object.keys(factors).length ? factors : null;
}
