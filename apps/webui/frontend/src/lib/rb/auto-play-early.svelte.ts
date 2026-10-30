/**
 * AutoPlay decides early and plans the handoff (AUTOPLAN-01..05).
 *
 * At the soft target AutoPlay asks the engine for a window ahead in the master and a track, loads it on the follower, gives
 * the pair time to render, and arms the path `>|` uses (armAutoPlayNext with from_ms), so saved plans, the M floor, variety
 * and save-on-run are the same code. The plan runner then owns the mix: auto-play.svelte.ts yields to any plan (AGENT-29).
 *
 * The old end-of-track handoff in auto-play.svelte.ts stays as the fallback. Everything short of a plan that completed ends
 * in `fallback` with a reason, and that handoff says the reason when it runs.
 *
 * Requirements:
 *   ✔︎ One decision per pass of a master track: Decide now or a seek back before the decision point asks for another.
 *   ✔︎ While a decision is working the old handoff waits, so the two never share the follower.
 *   ✔︎ A stale step does nothing: a new master, a disarm or a replaced deck ends the run.
 *   ✔︎ AutoPlay switched off cancels a plan that has not started, and leaves one that is running.
 * Acceptance:
 *   [if] the master is past the target [then] one plan is scheduled for the decided window
 *   [if] the plan fails or is cancelled [then] phase is fallback, the reason names it, and the cued track is kept
 *   [if] the follower is replaced while it prepares [then] nothing is armed, and the phase is fallback
 */
import { rustCommandUnsupported } from '$lib/audio-engine/rust-mode.svelte';
import { deckStates } from '$lib/rb/audio-engine.svelte';
import {
	armAutoPlayNext,
	autoPlayNextState,
	cancelAutoPlayNext,
	holdAutoPlayNextPrepare,
	recentAutoPlayStyles,
	readPlanStatus
} from '$lib/rb/auto-play-next.svelte';
import { requestPrepareStatus } from '$lib/rb/auto-play-next-plan';
import {
	buildShortlist,
	CUE_BUDGET_MS,
	decisionDueMs,
	earliestWindowMs,
	prepareWaitMs,
	requestDecision,
	SHORTLIST_SIZE,
	type DecidedWindow
} from '$lib/rb/auto-play-early';
import { autoPlayYieldsNow } from '$lib/rb/auto-play-plan-gate';
import { dispatchPerformanceCommand } from '$lib/rb/performance-ipc.svelte';
import { uiPrefs } from '$lib/rb/prefs.svelte';
import type { DeckId } from '$lib/rb/deck-slots';

const SETTLE_POLL_MS = 200;
/** A master with no follower or no shortlist yet is asked again this often, not every AutoPlay poll. */
const RETRY_MS = 5_000;

export type AutoPlayDecisionPhase =
	| 'idle'
	| 'deciding'
	| 'cueing'
	| 'preparing'
	| 'scheduled'
	| 'running'
	| 'done'
	| 'fallback';

export interface AutoPlayDecision {
	phase: AutoPlayDecisionPhase;
	source: string | null;
	/** Master position the decision is due at, ms. */
	due_ms: number | null;
	/** Master position it was taken at, ms. */
	decided_at_ms: number | null;
	track: string | null;
	in_deck: DeckId | null;
	window: DecidedWindow | null;
	style: string | null;
	bars: number | null;
	/** Window start minus decided_at_ms: the lead the quality signal reads. */
	lead_ms: number | null;
	plan_id: string | null;
	/** The last decision or the reason there is none. */
	reason: string | null;
}

function _idle(): AutoPlayDecision {
	return {
		phase: 'idle',
		source: null,
		due_ms: null,
		decided_at_ms: null,
		track: null,
		in_deck: null,
		window: null,
		style: null,
		bars: null,
		lead_ms: null,
		plan_id: null,
		reason: null
	};
}

export const autoPlayDecision = $state<AutoPlayDecision>(_idle());

export function readAutoPlayDecisionMirror(): AutoPlayDecision {
	const { window } = autoPlayDecision;
	return { ...autoPlayDecision, window: window === null ? null : { ...window } };
}

/** What one poll tells the decision about the master. */
export interface EarlySource {
	deck: DeckId;
	stable_id: string;
	position_ms: number;
	duration_ms: number | null;
	playing: boolean;
	bpm: number | null;
}

export interface EarlyPoll {
	source: EarlySource;
	follower: DeckId | null;
	/** Next-best pick past the tracks already taken, by AutoPlay's own rules. */
	pick: (taken: ReadonlySet<string>) => string | null;
	/** A decided track is spoken for: it goes in AutoPlay's claimed and played sets. */
	claim: (source: string, track: string) => void;
	stillArmed: () => boolean;
}

let _running: { deck: DeckId; source: string } | null = null;
let _force = false;
let _retryAtMs = 0;
let _loading = false;
/** Bumped by every new decision and cancel, so a stale await does nothing. */
let _token = 0;

export function autoPlayDecisionLoading(): boolean {
	return _loading;
}

function _replace(next: Partial<AutoPlayDecision>): void {
	Object.assign(autoPlayDecision, next);
}

function _endWithFallback(reason: string): void {
	console.info(`[autoplay] no planned handoff for ${autoPlayDecision.source?.slice(0, 12)}: ${reason}`);
	_replace({ phase: 'fallback', reason });
	_running = null;
}

function _stale(token: number, poll: EarlyPoll): string | null {
	if (token !== _token) return 'superseded';
	if (!poll.stillArmed()) return 'AutoPlay was switched off';
	if (deckStates[poll.source.deck].stable_id !== poll.source.stable_id) return 'the master changed track';
	if (autoPlayYieldsNow()) return 'another plan took the mix';
	return null;
}

async function _pause(ms: number): Promise<void> {
	await new Promise((resolve) => setTimeout(resolve, ms));
}

/** Put the track on the follower, unless it is there already. Throws what the deck refused. */
async function _cue(deck: DeckId, track: string): Promise<void> {
	const held = deckStates[deck].stable_id;
	if (held === track) return;
	if (held !== null) await dispatchPerformanceCommand({ type: 'unload', deck }, undefined, 'autoplay-handoff');
	await dispatchPerformanceCommand({ type: 'load', deck, stable_id: track }, undefined, 'autoplay-handoff');
}

async function _deckReady(deck: DeckId, track: string, deadlineMs: number): Promise<boolean> {
	for (;;) {
		const state = deckStates[deck];
		if (state.stable_id === track && state.anlz !== null && state.duration_ms !== null) return true;
		if (performance.now() >= deadlineMs) return false;
		await _pause(SETTLE_POLL_MS);
	}
}

/** Wait for the pair to render, to a limit: a press plans from what is ready. Only an answer about this track counts. */
async function _awaitPrepare(
	deck: DeckId,
	source: EarlySource,
	track: string,
	fromMs: number,
	waitMs: number,
	abandoned: () => boolean
): Promise<void> {
	const deadline = performance.now() + waitMs;
	while (performance.now() < deadline && !abandoned()) {
		const request = { out_deck: source.deck, in_deck: deck, intent: autoPlayNextState.intent, from_ms: fromMs };
		const status = await requestPrepareStatus(request).catch(
			(error: unknown) => {
				console.warn('auto-play: prepare status failed', error);
				return null;
			}
		);
		if (status !== null && status.in_track === track && status.state !== 'warming') return;
		await _pause(1000);
	}
}

async function _run(poll: EarlyPoll, shortlist: string[], follower: DeckId, targetMs: number, token: number): Promise<void> {
	const { source } = poll;
	const answer = await requestDecision({
		out_deck: source.deck,
		in_deck: follower,
		shortlist,
		intent: autoPlayNextState.intent,
		recent_styles: recentAutoPlayStyles(),
		earliest_ms: earliestWindowMs(source.position_ms, targetMs),
		target_ms: targetMs
	});
	const staleAfterAsk = _stale(token, poll);
	if (staleAfterAsk !== null) return _endWithFallback(staleAfterAsk);
	if (answer.track === null) return _endWithFallback(answer.reason);
	poll.claim(source.stable_id, answer.track);
	_replace({
		phase: 'cueing',
		track: answer.track,
		window: answer.window,
		style: answer.style,
		bars: answer.bars,
		lead_ms: Math.round(answer.window.start_ms - source.position_ms),
		reason: answer.reason
	});
	_loading = true;
	holdAutoPlayNextPrepare(true);
	try {
		await _cue(follower, answer.track);
	} catch (error) {
		_replace({ track: null });
		return _endWithFallback(`could not load the next track: ${error instanceof Error ? error.message : String(error)}`);
	} finally {
		_loading = false;
	}
	if (!(await _deckReady(follower, answer.track, performance.now() + CUE_BUDGET_MS * 2))) {
		return _endWithFallback(`deck ${follower} did not settle on the next track`);
	}
	_replace({ phase: 'preparing' });
	const taken = (): boolean => deckStates[follower].stable_id !== answer.track || deckStates[follower].playing;
	const waitMs = prepareWaitMs(answer.window.start_ms - deckStates[source.deck].position_ms);
	await _awaitPrepare(follower, source, answer.track, answer.window.start_ms, waitMs, () => taken() || _stale(token, poll) !== null);
	const stale = _stale(token, poll);
	if (stale !== null) return _endWithFallback(stale);
	if (taken()) return _endWithFallback(`deck ${follower} was taken while the transition prepared`);
	const armed = await armAutoPlayNext({ outDeck: source.deck, inDeck: follower, fromMs: answer.window.start_ms });
	if (token !== _token) return;
	if (!armed) return _endWithFallback(autoPlayNextState.run?.reason ?? 'the plan was not armed');
	// The planner has the last word, over rendered audio: the readout says what will play, not what was provisional.
	const { run } = autoPlayNextState;
	_replace({
		phase: 'scheduled',
		plan_id: readPlanStatus()?.id ?? null,
		style: run?.style ?? answer.style,
		bars: run?.bars ?? answer.bars,
		reason: run?.reason ?? answer.reason
	});
}

function _due(source: EarlySource): number | null {
	return decisionDueMs(source.duration_ms, uiPrefs.auto_play_decide_after_s * 1000, source.bpm);
}

function _start(poll: EarlyPoll, dueMs: number): void {
	const { source } = poll;
	if (rustCommandUnsupported('auto_play_next_arm')) {
		_replace({ source: source.stable_id });
		_endWithFallback('the Rust engine mode does not run planned transitions yet');
		return;
	}
	if (poll.follower === null) {
		_replace({ source: source.stable_id, reason: 'no free deck for the next track yet' });
		_retryAtMs = performance.now() + RETRY_MS;
		return;
	}
	const shortlist = buildShortlist(poll.pick, uiPrefs.auto_play_enforce_order ? 1 : SHORTLIST_SIZE);
	if (shortlist.length === 0) {
		_replace({ source: source.stable_id, reason: 'no playable next track in the feed yet' });
		_retryAtMs = performance.now() + RETRY_MS;
		return;
	}
	const follower = poll.follower;
	// Decide now asks for the next good point, so the soft target gives way to the playhead.
	const targetMs = _force ? source.position_ms : dueMs;
	_force = false;
	_token += 1;
	const token = _token;
	_running = { deck: follower, source: source.stable_id };
	_replace({ ..._idle(), phase: 'deciding', source: source.stable_id, due_ms: Math.round(dueMs), decided_at_ms: Math.round(source.position_ms), in_deck: follower });
	void _run(poll, shortlist, follower, targetMs, token)
		.catch((error: unknown) => {
			if (token === _token) _endWithFallback(error instanceof Error ? error.message : String(error));
		})
		.finally(() => holdAutoPlayNextPrepare(false));
}

/**
 * Follow the plan a decision armed, from the top of every AutoPlay poll: it must run while AutoPlay yields to that plan.
 * `autoPlayNextState.ended` is set when the plan leaves the runner.
 */
export function syncAutoPlayDecision(): void {
	const { phase } = autoPlayDecision;
	if (phase !== 'scheduled' && phase !== 'running') return;
	if (autoPlayNextState.armed) {
		if (readPlanStatus()?.status === 'running') _replace({ phase: 'running' });
		return;
	}
	const { ended } = autoPlayNextState;
	if (ended === 'done') {
		_replace({ phase: 'done' });
		_running = null;
		return;
	}
	_endWithFallback(ended === 'cancelled' ? 'the planned transition was cancelled' : `the planned transition ended ${ended ?? 'without a result'}`);
}

/** One AutoPlay poll. True while a decision holds the follower, so the old handoff must wait. */
export function pollAutoPlayDecision(poll: EarlyPoll): boolean {
	const { source } = poll;
	if (autoPlayDecision.source !== source.stable_id && _running === null) {
		_replace({ ..._idle(), source: source.stable_id });
		_retryAtMs = 0;
	}
	const { phase } = autoPlayDecision;
	if (phase === 'deciding' || phase === 'cueing' || phase === 'preparing' || phase === 'scheduled' || phase === 'running') {
		return true;
	}
	const dueMs = _due(source);
	if ((phase === 'done' || phase === 'fallback') && !_force) {
		// Seeked back before the decision point: this track gets another one, as the old handoff re-arms on a seek back.
		if (dueMs === null || source.position_ms >= dueMs) return false;
		_replace({ ..._idle(), source: source.stable_id });
	}
	// A person's own armed transition is left alone: AutoPlay would only be refused.
	if (autoPlayNextState.armed || !source.playing || performance.now() < _retryAtMs) return false;
	_replace({ due_ms: dueMs === null ? null : Math.round(dueMs) });
	if (dueMs === null || (!_force && source.position_ms < dueMs)) return false;
	_start(poll, dueMs);
	return autoPlayDecision.phase === 'deciding';
}

/** The track a failed decision left cued on this deck: the fallback plays it rather than picking again. */
export function autoPlayDecisionCuedTrack(deck: DeckId): string | null {
	const { phase, track, in_deck } = autoPlayDecision;
	return phase === 'fallback' && in_deck === deck && track !== null && deckStates[deck].stable_id === track ? track : null;
}

/** Why the old handoff is running now, for its log line. */
export function autoPlayFallbackReason(): string {
	if (!uiPrefs.auto_play_early_planning) return 'early planning is off';
	return autoPlayDecision.reason ?? `no decision was taken (${autoPlayDecision.phase})`;
}

/** The Decide now button: take the decision on the next poll, replacing one that has not started playing. */
export async function decideAutoPlayNow(): Promise<void> {
	if (!uiPrefs.auto_play_enabled) throw new Error('auto_play_decide_now: AutoPlay is off');
	if (autoPlayDecision.phase === 'running') throw new Error('auto_play_decide_now: a planned transition is running');
	await cancelAutoPlayDecision();
	_force = true;
	_retryAtMs = 0;
}

/** Drop the decision: a plan that has not started is aborted, one that is running plays out. */
export async function cancelAutoPlayDecision(): Promise<void> {
	_token += 1;
	_running = null;
	_loading = false;
	const { phase } = autoPlayDecision;
	if (phase === 'running') return;
	if (phase === 'scheduled') await cancelAutoPlayNext();
	_replace(_idle());
}
