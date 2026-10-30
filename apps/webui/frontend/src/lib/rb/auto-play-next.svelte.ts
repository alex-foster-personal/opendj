/**
 * AutoPlay Next orchestrator (pin fc60002b81a8): the thin, stateful half of
 * the featurette. All decision math lives in auto-play-next.ts (pure,
 * fully unit-tested); this file only reads deckStates, polls on the same
 * $effect.root + setInterval shape auto-play.svelte.ts already uses, and
 * dispatches ordinary performance commands (beat_loop, loop, eq) - no new
 * engine surface, audio-engine.svelte.ts is untouched.
 *
 * PLAY-11 (issue #3532): beat_loop on a downbeat, guaranteed loop release.
 *
 * AGENT-48: arming asks the Python chooser first (auto-play-next-plan.ts) and runs its order on the page's plan
 * runner. The featurette below is the fallback for a pair the planner has no pick for.
 *
 * v1 hard-cut, deliberately imperfect (own file/featurette per the pin):
 * armed state is in-memory only (no persisted pref), one outgoing/incoming
 * pair at a time, and a failed precondition (no anlz, no beatgrid, no
 * follower loaded) just refuses to arm rather than degrading further.
 */
import { DECK_IDS, deckStates } from '$lib/rb/audio-engine.svelte';
import {
	approximateDropMs,
	AUTO_PLAY_NEXT_MAX_LOOP_MS,
	bassEntryMs,
	DEFAULT_AUTO_PLAY_NEXT_CONFIG,
	duckedLowEqKnob,
	findRepetitiveLoopWindow,
	planAutoPlayNextBeatLoop,
	type AutoPlayNextConfig
} from '$lib/rb/auto-play-next';
import {
	PLANNER_TIMEOUT_MS,
	VARIETY_WINDOW,
	planRunOf,
	requestPlannedTransition,
	requestPrepare,
	reportPlayed,
	type AutoPlayNextRun,
	type TransitionChoice,
	type TransitionIntent
} from '$lib/rb/auto-play-next-plan';
export { runLabel, runTitle, TRANSITION_INTENTS } from '$lib/rb/auto-play-next-plan';
export type { TransitionIntent } from '$lib/rb/auto-play-next-plan';
import { pickSourceDeck, type AutoPlayDeckSnap } from '$lib/rb/auto-play';
import { dispatchPerformanceCommand } from '$lib/rb/performance-ipc.svelte';
import { pushToast } from '$lib/stores.svelte';
import type { DeckId } from '$lib/rb/deck-slots';

const POLL_MS = 250;

/** The page's plan runner, as AutoPlay Next needs it. Registered by the route, which owns the runner's lifetime. */
export interface AutoPlayNextPlanRunner {
	schedule(plan: Record<string, unknown>): Promise<{ failed: string | null }>;
	abort(planId: string): Promise<void>;
	status(): { id: string; status: string } | null;
}

let _planRunner: AutoPlayNextPlanRunner | null = null;

export function registerAutoPlayNextPlanRunner(runner: AutoPlayNextPlanRunner): () => void {
	_planRunner = runner;
	return () => {
		if (_planRunner === runner) _planRunner = null;
	};
}

type Phase = 'idle' | 'planning' | 'planned' | 'looping' | 'ducked' | 'done';

export const autoPlayNextState = $state<{
	armed: boolean;
	outgoing: DeckId | null;
	incoming: DeckId | null;
	phase: Phase;
	intent: TransitionIntent;
	/** Outlives the arm, so the readout still says which path ran once the transition is over. */
	run: AutoPlayNextRun | null;
	/** How the last plan left the runner: done, failed, aborted, cancelled, or gone when the runner dropped it. */
	ended: string | null;
}>({ armed: false, outgoing: null, incoming: null, phase: 'idle', intent: 'smooth', run: null, ended: null });

/** The status API's copy of the state above (`auto_play_next` on the UI mirror). */
export function readAutoPlayNextMirror(): {
	armed: boolean;
	phase: Phase;
	intent: TransitionIntent;
	run: AutoPlayNextRun | null;
} {
	const { armed, phase, intent, run } = autoPlayNextState;
	return { armed, phase, intent, run: run === null ? null : { ...run } };
}

/** The runner's current plan, for AutoPlay's early decision. */
export function readPlanStatus(): { id: string; status: string } | null {
	return _planRunner?.status() ?? null;
}

export function recentAutoPlayStyles(): string[] {
	return [..._recentStyles];
}

let _timer: ReturnType<typeof setInterval> | null = null;
let _dropMs: number | null = null;
let _loopEngaged = false;
let _eqDucked = false;
let _loopingSinceMs: number | null = null;
let _releaseInFlight: Promise<void> | null = null;
/** Bumped by every arm and cancel, so a planner answer that arrives after a cancel is dropped. */
let _armGeneration = 0;
let _plannerAbort: AbortController | null = null;
let _planId: string | null = null;
/** AGENT-49: what to report when the running plan completes. */
let _playing: { out: string; in: string; saved: boolean; plan: Record<string, unknown> | null; style: string } | null = null;
/** Styles the planner path has played this page session, oldest first: the chooser's variety memory. */
const _recentStyles: string[] = [];

function _snaps(): AutoPlayDeckSnap[] {
	return DECK_IDS.map((id) => {
		const d = deckStates[id];
		return {
			id,
			stable_id: d.stable_id,
			playing: d.playing,
			position_ms: d.position_ms,
			duration_ms: d.duration_ms,
			is_master: d.is_master,
			beat_sync_enabled: d.beat_sync_enabled
		};
	});
}

function _reset(): void {
	if (_timer !== null) {
		clearInterval(_timer);
		_timer = null;
	}
	_dropMs = null;
	_loopEngaged = false;
	_eqDucked = false;
	_loopingSinceMs = null;
	_planId = null;
	_playing = null;
	autoPlayNextState.armed = false;
	autoPlayNextState.outgoing = null;
	autoPlayNextState.incoming = null;
	autoPlayNextState.phase = 'idle';
}

async function _releaseOutgoing(deck: DeckId | null, restoreEq: boolean): Promise<void> {
	if (deck === null) {
		_loopEngaged = false;
		_eqDucked = false;
		return;
	}
	if (_releaseInFlight !== null) {
		await _releaseInFlight;
		return;
	}
	_releaseInFlight = (async () => {
		if (_loopEngaged) {
			await dispatchPerformanceCommand({ type: 'loop', deck, loop: null }, undefined, 'autoplay-handoff');
			_loopEngaged = false;
		}
		if (restoreEq && _eqDucked) {
			await dispatchPerformanceCommand({ type: 'eq', deck, band: 'low', value: 0.5 }, undefined, 'autoplay-handoff');
			_eqDucked = false;
		}
	})();
	try {
		await _releaseInFlight;
	} finally {
		_releaseInFlight = null;
	}
}

async function _finishTransition(restoreEq: boolean): Promise<void> {
	const outgoing = autoPlayNextState.outgoing;
	await _releaseOutgoing(outgoing, restoreEq);
	autoPlayNextState.phase = 'done';
	_reset();
}

async function _abortTransition(toast: string | null): Promise<void> {
	const outgoing = autoPlayNextState.outgoing;
	const restoreEq = autoPlayNextState.phase === 'ducked';
	await _releaseOutgoing(outgoing, restoreEq);
	if (toast !== null) pushToast(toast, 'info');
	_reset();
}

interface NextPair {
	outgoing: DeckId;
	incoming: DeckId;
	/** AGENT-50: leave the outgoing track near this point (ms on its own clock), not at the planner's best window. */
	fromMs?: number | undefined;
	/** AGENT-50: a person named this deck, so a playing one is taken over. `>|` never takes a deck. */
	mayTakeIncoming: boolean;
}

/** What one arm asks for: the `>|` button names nothing; a waveform right-click names both decks and the point. */
export interface AutoPlayNextRequest {
	intent?: TransitionIntent | undefined;
	outDeck?: DeckId | undefined;
	inDeck?: DeckId | undefined;
	fromMs?: number | undefined;
}

/** A deck a transition may go to: a loaded track that has not played to its end. A playing deck is allowed (AGENT-50:
 * the transition takes it over). */
export function transitionTargetOk(deck: DeckId): boolean {
	const state = deckStates[deck];
	if (state.stable_id === null) return false;
	const ended = state.duration_ms !== null && !state.playing && state.position_ms >= state.duration_ms - 500;
	return !ended;
}

function _chosenPair(request: AutoPlayNextRequest): NextPair | null {
	if (request.outDeck === undefined && request.inDeck === undefined) return _nextPair();
	if (request.outDeck === undefined || request.inDeck === undefined || request.outDeck === request.inDeck) {
		pushToast('transition: name two different decks', 'error');
		return null;
	}
	if (deckStates[request.outDeck].stable_id === null || !transitionTargetOk(request.inDeck)) {
		pushToast(`transition: CH${request.outDeck} or CH${request.inDeck} holds no track to mix`, 'info');
		return null;
	}
	return { outgoing: request.outDeck, incoming: request.inDeck, fromMs: request.fromMs, mayTakeIncoming: true };
}

function _nextPair(): NextPair | null {
	const snaps = _snaps();
	const outgoing = pickSourceDeck(snaps);
	if (outgoing === null) {
		pushToast('auto-play next: no master deck to loop', 'info');
		return null;
	}
	const incoming = snaps.find((s) => s.id !== outgoing.id && s.stable_id !== null) ?? null;
	if (incoming === null) {
		pushToast('auto-play next: no loaded incoming deck to hand off to', 'info');
		return null;
	}
	return { outgoing: outgoing.id, incoming: incoming.id, mayTakeIncoming: false };
}

async function _askPlanner(pair: NextPair): Promise<TransitionChoice> {
	if (_planRunner === null) return { path: 'featurette', reason: 'no plan runner on this page' };
	_plannerAbort = new AbortController();
	const timeout = setTimeout(() => _plannerAbort?.abort(), PLANNER_TIMEOUT_MS);
	try {
		return await requestPlannedTransition(
			{
				out_deck: pair.outgoing,
				in_deck: pair.incoming,
				intent: autoPlayNextState.intent,
				recent_styles: [..._recentStyles],
				...(pair.fromMs === undefined ? {} : { from_ms: pair.fromMs }),
				...(pair.mayTakeIncoming && deckStates[pair.incoming].playing ? { take_incoming: true } : {})
			},
			_plannerAbort.signal
		);
	} finally {
		clearTimeout(timeout);
		_plannerAbort = null;
	}
}

/** Hand the order to the plan runner, or say why it would not take it. */
async function _startPlan(plan: Record<string, unknown>, generation: number, pair: NextPair): Promise<string | null> {
	if (_planRunner === null) return 'the plan runner left this page';
	const runner = _planRunner;
	const taken = pair.mayTakeIncoming && deckStates[pair.incoming].playing;
	if (taken) {
		// AGENT-50: the user chose a deck that is playing; the transition takes it over, stopped and re-cued by the plan.
		await dispatchPerformanceCommand({ type: 'play', deck: pair.incoming, playing: false }, undefined, 'autoplay-handoff');
	}
	const { failed } = await runner.schedule(plan);
	if (failed !== null) {
		// The plan never started, so the deck it took goes back to playing.
		if (taken) await dispatchPerformanceCommand({ type: 'play', deck: pair.incoming, playing: true }, undefined, 'autoplay-handoff');
		return failed;
	}
	if (generation !== _armGeneration) {
		// Cancelled while the runner was taking it: the plan is armed now and nothing here owns it, so abort it.
		await runner.abort(String(plan.id));
		if (taken) await dispatchPerformanceCommand({ type: 'play', deck: pair.incoming, playing: true }, undefined, 'autoplay-handoff');
		return 'cancelled';
	}
	_planId = String(plan.id);
	autoPlayNextState.phase = 'planned';
	_timer = setInterval(_watchPlan, POLL_MS);
	return null;
}

function _watchPlan(): void {
	const plan = _planRunner?.status() ?? null;
	if (plan === null || plan.id !== _planId) {
		autoPlayNextState.ended = 'gone';
		_reset();
		return;
	}
	if (plan.status === 'scheduled' || plan.status === 'running') return;
	autoPlayNextState.ended = plan.status;
	const style = autoPlayNextState.run?.style;
	if (plan.status === 'done' && style) {
		_recentStyles.push(style);
		_recentStyles.splice(0, Math.max(0, _recentStyles.length - VARIETY_WINDOW));
		if (_playing !== null) {
			reportPlayed(_playing).catch((error: unknown) => {
				console.warn('auto-play next: could not save the transition for this pair', error);
			});
		}
	}
	if (plan.status === 'failed') pushToast(`auto-play next: planned transition failed`, 'error');
	_reset();
}

/**
 * Arm AutoPlay Next: ask the planner for a transition and run it on the plan runner; with no pick, fall back to the
 * featurette. Refuses (returns false, toasts why) rather than guessing when no pair of decks can hand over.
 */
export async function armAutoPlayNext(
	request: AutoPlayNextRequest = {},
	config: AutoPlayNextConfig = DEFAULT_AUTO_PLAY_NEXT_CONFIG
): Promise<boolean> {
	if (autoPlayNextState.armed) return false;
	const pair = _chosenPair(request);
	if (pair === null) return false;
	if (request.intent !== undefined) autoPlayNextState.intent = request.intent;
	_armGeneration += 1;
	const generation = _armGeneration;
	autoPlayNextState.armed = true;
	autoPlayNextState.outgoing = pair.outgoing;
	autoPlayNextState.incoming = pair.incoming;
	autoPlayNextState.phase = 'planning';
	autoPlayNextState.run = null;
	autoPlayNextState.ended = null;
	const armedAt = performance.now();
	const choice = await _askPlanner(pair);
	if (generation !== _armGeneration) return false;
	const run = planRunOf(choice, performance.now() - armedAt);
	autoPlayNextState.run = run;
	if (choice.path === 'planner') {
		const out = deckStates[pair.outgoing].stable_id;
		const incoming = deckStates[pair.incoming].stable_id;
		const refused = await _startPlan(choice.plan, generation, pair);
		if (generation !== _armGeneration) return false;
		if (refused === null) {
			if (out !== null && incoming !== null) {
				_playing = { out, in: incoming, saved: choice.saved, plan: choice.document, style: choice.pick.style };
			}
			return true;
		}
		autoPlayNextState.run = { ...run, path: 'featurette', reason: `plan refused: ${refused}` };
	}
	if (pair.fromMs !== undefined) {
		// AGENT-50: the user named a point; the fixed loop leaves from the track's end, which is not what was asked.
		autoPlayNextState.run = { ...autoPlayNextState.run, path: 'none', reason: `no planned transition here: ${autoPlayNextState.run.reason}` };
		_reset();
		return false;
	}
	let armed = false;
	try {
		armed = await _armFeaturette(pair, config);
	} finally {
		// A refusal or a throw (a track with no beatgrid) must not leave the button armed with nothing running.
		if (!armed) _reset();
	}
	return armed;
}

/**
 * The fixed featurette: find the outgoing deck's last repetitive 8-beat window and loop it on a downbeat via
 * beat_loop. Refuses (returns false, toasts why) when either deck lacks the analysis this needs.
 */
async function _armFeaturette(pair: NextPair, config: AutoPlayNextConfig): Promise<boolean> {
	const outgoingAnlz = deckStates[pair.outgoing].anlz;
	const outgoingDuration = deckStates[pair.outgoing].duration_ms;
	if (outgoingAnlz === null || outgoingDuration === null) {
		pushToast('auto-play next: outgoing deck has no analysis to loop', 'info');
		return false;
	}
	const window = findRepetitiveLoopWindow(
		outgoingAnlz.waveform,
		outgoingAnlz.beatgrid.beats,
		outgoingDuration / 1000,
		config
	);
	if (window === null) {
		pushToast('auto-play next: no repetitive 8-beat window found to loop', 'info');
		return false;
	}
	const plan = planAutoPlayNextBeatLoop(
		outgoingAnlz.beatgrid.beats,
		window,
		outgoingDuration / 1000
	);
	if (plan === null) {
		pushToast('auto-play next: no downbeat-aligned 8-beat loop window', 'info');
		return false;
	}
	try {
		await dispatchPerformanceCommand({
			type: 'beat_loop',
			deck: pair.outgoing,
			beats: plan.beats,
			start_ms: plan.start_ms
		}, undefined, 'autoplay-handoff');
	} catch {
		pushToast('auto-play next: loop engage failed', 'error');
		return false;
	}
	_loopEngaged = true;
	_loopingSinceMs = Date.now();
	autoPlayNextState.phase = 'looping';
	_dropMs = null;
	_timer = setInterval(() => {
		void _tick(config);
	}, POLL_MS);
	return true;
}

/** How often the pair on the decks is compared with the last one prepared. Reads two deck states; no engine call. */
const PREPARE_WATCH_MS = 1000;
let _prepareHeld = false;

/** AUTOPLAN-04: AutoPlay's decision prepares its own pair, around the window it chose, so the watch must not get there first. */
export function holdAutoPlayNextPrepare(held: boolean): void {
	_prepareHeld = held;
}

/**
 * AGENT-52: plan ahead. Once a track sits on the idle deck, ask the engine to score the pair, so a press reads a cache
 * instead of waiting on renders. A failed request is logged and the press plans from the profiles.
 */
export function installAutoPlayNextPrepare(): () => void {
	let prepared: string | null = null;
	const timer = setInterval(() => {
		if (_prepareHeld) return;
		const snaps = _snaps();
		const outgoing = pickSourceDeck(snaps);
		const incoming = outgoing === null ? null : snaps.find((s) => s.id !== outgoing.id && s.stable_id !== null && !s.playing);
		if (outgoing === null || incoming === null || incoming === undefined) {
			prepared = null;
			return;
		}
		const { intent } = autoPlayNextState;
		const key = `${outgoing.stable_id}|${incoming.stable_id}|${outgoing.id}|${incoming.id}|${intent}`;
		if (key === prepared) return;
		prepared = key;
		requestPrepare({ out_deck: outgoing.id, in_deck: incoming.id, intent }).catch((error: unknown) => {
			// Asked again on the next watch: a refused request has prepared nothing.
			if (prepared === key) prepared = null;
			console.warn('auto-play next: prepare request failed', error);
		});
	}, PREPARE_WATCH_MS);
	return () => clearInterval(timer);
}

/** Cancel AutoPlay Next: drop a planner request, abort a planned transition, or release the loop and any EQ duck. */
export async function cancelAutoPlayNext(): Promise<void> {
	if (!autoPlayNextState.armed) return;
	_armGeneration += 1;
	autoPlayNextState.ended = 'cancelled';
	_plannerAbort?.abort();
	if (autoPlayNextState.phase === 'planning') {
		_reset();
		return;
	}
	if (_planId !== null) {
		// Reset only once the abort is in: a refused abort leaves the plan running, and it stays cancellable.
		await _planRunner?.abort(_planId);
		_reset();
		return;
	}
	const restoreEq = autoPlayNextState.phase === 'ducked';
	await _finishTransition(restoreEq);
}

async function _tick(config: AutoPlayNextConfig): Promise<void> {
	const incoming = autoPlayNextState.incoming;
	const outgoing = autoPlayNextState.outgoing;
	if (incoming === null || outgoing === null) return;
	const outgoingState = deckStates[outgoing];
	if (outgoingState.stable_id === null || !outgoingState.playing) {
		await _abortTransition(null);
		return;
	}
	const incomingAnlz = deckStates[incoming].anlz;
	const incomingDuration = deckStates[incoming].duration_ms;
	if (incomingAnlz === null || incomingDuration === null) return;
	const beats = incomingAnlz.beatgrid.beats;
	const positionMs = deckStates[incoming].position_ms;

	if (autoPlayNextState.phase === 'looping') {
		if (_loopingSinceMs !== null && Date.now() - _loopingSinceMs > AUTO_PLAY_NEXT_MAX_LOOP_MS) {
			await _abortTransition('auto-play next: bass entry timeout, loop released');
			return;
		}
		const enteredMs = bassEntryMs(
			incomingAnlz.waveform,
			beats,
			incomingDuration / 1000,
			positionMs,
			config
		);
		if (enteredMs === null) return;
		await dispatchPerformanceCommand({
			type: 'eq',
			deck: outgoing,
			band: 'low',
			value: duckedLowEqKnob(config.lowEqDuckFraction)
		}, undefined, 'autoplay-handoff');
		_eqDucked = true;
		_dropMs = approximateDropMs(beats, enteredMs, config);
		if (_dropMs === null) {
			await _finishTransition(true);
			return;
		}
		autoPlayNextState.phase = 'ducked';
		return;
	}

	if (autoPlayNextState.phase === 'ducked') {
		if (_dropMs === null || positionMs >= _dropMs) {
			await _finishTransition(true);
		}
	}
}
