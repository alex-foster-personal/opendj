/**
 * AGENT-24: the page's transition-plan executor.
 *
 * The canon AutoDJ doc ((private path))
 * splits a transition into planner, executor and critic: an agent (or the
 * deterministic chooser in apps/autodj) decides, and the executor performs a
 * whole transition end to end. This module is that executor. It does no
 * musical reasoning: `apps/autodj/compile.py` hands it bus commands keyed to
 * bars on the CLOCK deck (the outgoing deck), and it
 *
 *   - runs `prepare` at once (cue the incoming deck, Beat Sync, initial kills),
 *   - fires each event when the clock deck's playhead crosses its bar, and
 *   - moves each ramp against the live playhead, like the AGENT-03 ramp order.
 *
 * Unlike a `ramp` order, a plan does NOT hold the order bus open: `schedule`
 * answers once the plan is armed, so `plan_amend` (abort, finish_now, extend)
 * can reach it while it runs, and other orders keep flowing. Progress is
 * published on the UI mirror as `agent_plan` (`readAgentPlanMirror`).
 *
 * Bars are counted on the clock deck's DOWNBEATS (`n === 1`), the same rule
 * agent-duration.ts uses for a `bars` Duration and apps/autodj/profile.py uses
 * for its bar numbers, so "bar 113" names one moment on all three.
 *
 * Known limit: the tick is a 16 ms timer, so a HIDDEN tab's throttled timers
 * (1 s, then 60 s) slow it the same way they slow a `ramp` order. The desktop
 * shell's webview is visible while a set plays.
 */

import { barAt, downbeatsMs, msAtBar } from './plan-bars';
import { alignmentChanged, buildPlanOverlay, failedPlanOverlay, incomingAlignment, type IncomingAlignment, type PlanOverlay, type PlanOverlayInput } from './plan-overlay';

export { barAt, downbeatsMs, msAtBar };

type DeckId = 1 | 2 | 3 | 4;
/** A bus command as the plan carries it; the caller's `dispatch` validates it (AGENT-03). */
export type PlanCommand = Record<string, unknown>;
const DECKS: readonly DeckId[] = [1, 2, 3, 4];

/** Only these command types may appear in a plan: transport, mixer, tempo. */
const PLAN_COMMAND_TYPES: ReadonlySet<string> = new Set([
	'play',
	'master',
	'seek',
	'beat_sync',
	'fader',
	'trim',
	'filter',
	'eq',
	'crossfader',
	'tempo'
]);
/** The command types a plan may ramp (crossfader and tempo included, AGENT-24). */
const PLAN_RAMP_TYPES: ReadonlySet<string> = new Set(['fader', 'trim', 'filter', 'eq', 'crossfader', 'tempo']);
/** What `prepare` may carry: the compiler's setup (cue, tempo, Beat Sync, hoisted incoming-deck sets), never a transport play. */
const PREPARE_TYPES: ReadonlySet<string> = new Set(['master', 'seek', 'beat_sync', 'tempo', 'fader', 'trim', 'filter', 'eq']);
/** Setup the compiler emits only into `prepare`: a seek or Beat Sync fired mid-plan jumps the audible deck or re-syncs it. */
const SETUP_ONLY_TYPES: ReadonlySet<string> = new Set(['seek', 'beat_sync']);
const PLAN_TICK_MS = 16;
/**
 * AGENT-30: step sets (a fader cut, an EQ kill or swap) fire on the AUDIO clock, not on the tick. A step whose bar
 * is this close is dispatched early with `at_context_sec`, the AudioContext time of its bar, so the bus starts it on
 * that sample. 100 ms covers a 16 ms tick plus main-thread jitter and stays under one beat at 300 BPM, so a launch
 * one beat early (LATENCY-02) is always armed before the cut that follows it.
 */
export const STEP_LOOKAHEAD_S = 0.1;

/** How far a sent-ahead step's re-derived time may drift (float noise, one anchor to the next) before it counts as moved. */
const TIMELINE_TOLERANCE_S = 0.002;

/** Ramps that change whether a deck is heard, so a fader step judged against the room waits for them. */
const ROOM_RAMP_TYPES = new Set(['fader', 'trim', 'crossfader']);
/** The step sets the bus can start on an AudioContext time (`at_context_sec` on fader and eq, ADR on the scheduled set). */
const SCHEDULABLE_STEP_TYPES: ReadonlySet<string> = new Set(['fader', 'eq']);
/** How long the finished/aborted plan stays on the mirror, for an agent to read. */
export const PLAN_MIRROR_KEEP_MS = 5 * 60_000;

interface PlanCommandEvent {
	at_bar: number;
	command: Record<string, unknown>;
}

interface PlanRampEvent {
	at_bar: number;
	ramp: { command: Record<string, unknown>; to: number; over_bars: number };
}

type PlanEvent = PlanCommandEvent | PlanRampEvent;

interface AgentPlanOrder {
	id: string;
	clock: { deck: DeckId; stable_id: string };
	incoming: { deck: DeckId; stable_id: string } | null;
	start_bar: number;
	bars: number;
	style: string | null;
	/** AGENT-25: the session is muted, so the plan's audible plays need the master muted when they fire. */
	muted: boolean;
	prepare: Record<string, unknown>[];
	events: PlanEvent[];
	/** What the compiler decided and says (an uncapped outgoing fader, say); for the agent, never acted on. */
	notes: string[];
}

export type AgentPlanStatus = 'scheduled' | 'running' | 'done' | 'aborted' | 'failed';

export interface AgentPlanMirror {
	id: string;
	style: string | null;
	status: AgentPlanStatus;
	clock_deck: DeckId;
	incoming_deck: DeckId | null;
	start_bar: number;
	bars: number;
	/** The plan's own bar (0 = start_bar on the clock deck); null before any tick. */
	bar: number | null;
	fired: number;
	total: number;
	reason: string | null;
	/** WAVEPLAN-7896c: the curves on each deck's track time while the plan is pending, else null. */
	overlay: PlanOverlay | null;
}

type PlanAmendAction = 'abort' | 'finish_now' | 'extend';

interface PlanDeckView {
	stable_id: string | null;
	playing: boolean;
	position_ms: number;
	beatgrid: readonly { n: number; time_ms: number }[];
	pitch: number;
	/** The deck's live pitch range in percent (8, 16 or 100), read before each tempo ramp step. */
	pitch_range?: number | null;
	/** The deck's effective tempo in BPM (`queryPerformanceState`), or null when unknown. */
	effective_bpm?: number | null;
	/** A quantized play waiting for its downbeat (`queryPerformanceState`), else null. */
	quantized_launch_armed?: { launch_at_context_sec?: number } | null;
}

/** Armed but not yet started: the play fired, the deck starts on its next downbeat. */
function _armedOnly(deck: PlanDeckView): boolean {
	return !deck.playing && deck.quantized_launch_armed != null;
}

/**
 * Incoming grid bars the incoming deck consumes per clock bar: the ratio of the two effective tempos, which is
 * 2 or 0.5 when Beat Sync folds the tempo, else 1. Null when either tempo is unknown, so the caller refuses
 * rather than assume bars are preserved.
 */
/**
 * The most incoming grid bars one clock bar can consume while the plan's authored tempos run: each deck's effective
 * tempo is its raw tempo at its current pitch, so the rate is the current ratio rescaled to the fastest incoming
 * target over the slowest clock target. Null when a tempo or pitch is unknown, so the caller refuses.
 */
function _gridBarsPerClockBar(
	clock: PlanDeckView,
	incoming: PlanDeckView,
	targets: { clock: readonly number[]; incoming: readonly number[] } = { clock: [], incoming: [] },
): number | null {
	const clockBpm = clock.effective_bpm;
	const incomingBpm = incoming.effective_bpm;
	if (typeof clockBpm !== 'number' || typeof incomingBpm !== 'number' || clockBpm <= 0 || incomingBpm <= 0) return null;
	if (!(clock.pitch > 0) || !(incoming.pitch > 0)) return null;
	const fastestIncoming = Math.max(incoming.pitch, ...targets.incoming);
	const slowestClock = Math.min(clock.pitch, ...targets.clock);
	return (incomingBpm / incoming.pitch) * fastestIncoming / ((clockBpm / clock.pitch) * slowestClock);
}

const OVERLAY_KNOBS = ['fader', 'eq_low', 'eq_mid', 'eq_high', 'filter'] as const;
const _downbeatCache = new WeakMap<object, number[]>();

/** A deck's downbeats, once per grid: the overlay reads them every tick. */
function _downbeatsOf(deck: PlanDeckView): number[] {
	let downbeats = _downbeatCache.get(deck.beatgrid);
	if (downbeats === undefined) {
		downbeats = downbeatsMs(deck.beatgrid);
		_downbeatCache.set(deck.beatgrid, downbeats);
	}
	return downbeats;
}

/** The ratios the plan's own tempo commands and ramps set on one deck, from the order's events. */
function _tempoTargets(events: readonly PlanEvent[], deck: DeckId | undefined): number[] {
	return events.flatMap((event) => {
		if ('ramp' in event) {
			return event.ramp.command.type === 'tempo' && event.ramp.command.deck === deck ? [event.ramp.to] : [];
		}
		return event.command.type === 'tempo' && event.command.deck === deck && typeof event.command.ratio === 'number'
			? [event.command.ratio]
			: [];
	});
}

/** `TRIM_MAX_GAIN` and `SILENCE_GAIN_EPSILON` from player/constants.ts and master-election.ts (not imported: this module is standalone). */
const TRIM_MAX_GAIN = 2;
const SILENCE_GAIN_EPSILON = 1e-4;

/**
 * Whether a channel's on-air gain is silent: fader, trim, crossfader side and master multiplied the way the
 * engine does (master-election's onAirGain). A closed path would make the outgoing deck's stop silence the room.
 */
function _channelSilent(channel: Record<string, unknown>, crossfader: number, master: number): boolean {
	const { assign, fader, trim } = channel;
	if (typeof fader !== 'number' || typeof trim !== 'number' || typeof assign !== 'string') {
		throw new Error('mixer channel has no fader, trim or crossfader assignment');
	}
	const side =
		assign === 'THRU'
			? 1
			: assign === 'A'
				? Math.cos((crossfader * Math.PI) / 2)
				: Math.cos(((1 - crossfader) * Math.PI) / 2);
	return TRIM_MAX_GAIN * trim * fader * side * master < SILENCE_GAIN_EPSILON;
}

export interface PlanStateView {
	decks: Record<DeckId, PlanDeckView>;
	/** The master deck the page has elected: a quantized launch aligns to it, so it must still be the clock deck. */
	master_deck?: DeckId | null;
	/** The page's master mute: a muted plan's play sounds through the master unless it still holds. */
	master?: { muted: boolean };
	mixer: { crossfader: number; master: number; channels: Record<DeckId, object> };
}

interface PlanDeps {
	dispatch: (command: PlanCommand) => Promise<unknown>;
	query: () => PlanStateView;
	setTimer: (callback: () => void, ms: number) => unknown;
	clearTimer: (handle: unknown) => void;
	now: () => number;
	/** AutoPlay has a handoff loading or starting a deck: a plan accepted now would share that deck. */
	handoffInFlight: () => boolean;
	/** The engine's AudioContext clock in seconds: the lookahead and a scheduled step's landing are read on it (AGENT-30). */
	contextNowSec: () => number;
	/**
	 * AGENT-30: a deck's playhead and the AudioContext time it was presented at, from ONE engine observation, so a
	 * scheduled step's time is never a stale playhead added to a later clock read. Null before the first observation.
	 */
	positionAnchor: (deck: DeckId) => { position_ms: number; context_sec: number; pitch: number } | null;
	/**
	 * AGENT-30 (Sol P1 #7594): a scheduled step's state as the ENGINE owns it, by the `step_id` it was sent with:
	 * `waiting` until its mirror write applies on the audio clock, `landed` after, `cancelled` once a plain set on its
	 * control dropped it, `lost` once the engine session ended while it waited; null when this engine never saw it or
	 * the runner already forgot it.
	 */
	stepState: (stepId: string) => 'waiting' | 'landed' | 'cancelled' | 'lost' | null;
	/** AGENT-30 (Sol P1 #7594): the runner has retired the step, so the engine may drop its finished state. */
	forgetStep: (stepId: string) => void;
	/**
	 * Whether a deck's ready stem parts are all muted or gained to zero, so it is silent whatever its channel reads.
	 * The page answers with `everyStemPartSilent`; a deck with no stem bundle is never silent this way.
	 */
	stemsSilent: (deck: DeckId) => boolean;
	/** WAVEPLAN-7896: the waveform's overlay changed: a new geometry, or null once no plan is pending. */
	onOverlay?: (overlay: PlanOverlay | null) => void;
}

// ----------------------------------------------------------------- parse ---

function _record(value: unknown, where: string): Record<string, unknown> {
	if (value === null || typeof value !== 'object' || Array.isArray(value)) {
		throw new TypeError(`${where} must be an object`);
	}
	return value as Record<string, unknown>;
}

function _finite(value: unknown, where: string): number {
	if (typeof value !== 'number' || !Number.isFinite(value)) throw new TypeError(`${where} must be a finite number`);
	return value;
}

function _deck(value: unknown, where: string): DeckId {
	if (!DECKS.includes(value as DeckId)) throw new RangeError(`${where} must be a deck 1..4`);
	return value as DeckId;
}

function _stableId(value: unknown, where: string): string {
	if (typeof value !== 'string' || value === '') throw new TypeError(`${where} must be a stable_id`);
	return value;
}

/** AGENT-25: a muted agent session mutes the master in `prepare`. A plan may
 *  only ever mute (never unmute), and only there, never as a timed event. Any
 *  other prepare command is setup on the incoming deck, or selects the clock
 *  deck as master: a play or a clock-side mixer change would stop the room. */
function _prepareCommand(
	value: unknown,
	where: string,
	clockDeck: DeckId,
	incomingDeck: DeckId | null
): Record<string, unknown> {
	const command = _record(value, where);
	if (command.type === 'master_mute') {
		if (command.muted !== true) throw new TypeError(`${where}: a plan may mute the master, never unmute it`);
		// A plan's mute is for this run only: the dispatcher persists a mute unless persist is false, so pin it.
		if (command.persist !== undefined && command.persist !== false) {
			throw new TypeError(`${where}: a plan's mute is run-only, so persist must be false`);
		}
		return { ...command, persist: false };
	}
	const planCommand = _planCommand(command, where);
	if (!PREPARE_TYPES.has(planCommand.type as string)) {
		throw new TypeError(`${where}.type must be one of ${[...PREPARE_TYPES].join(', ')}`);
	}
	if (planCommand.type === 'master') {
		if (planCommand.deck !== clockDeck) throw new TypeError(`${where}: prepare may select only the clock deck as master`);
	} else if (incomingDeck === null || planCommand.deck !== incomingDeck) {
		throw new TypeError(`${where}: prepare may set only the incoming deck`);
	}
	return planCommand;
}

function _planCommand(value: unknown, where: string): Record<string, unknown> {
	const command = _record(value, where);
	if (typeof command.type !== 'string' || !PLAN_COMMAND_TYPES.has(command.type)) {
		throw new TypeError(`${where}.type must be one of ${[...PLAN_COMMAND_TYPES].join(', ')}`);
	}
	return command;
}

/** A timed command may touch only the plan's own two decks: the clock and the incoming. Any other deck is a
 *  raw order reaching a deck the plan never named, so it is refused before it can dispatch. */
function _planDeckCommand(command: Record<string, unknown>, where: string, clockDeck: DeckId, incomingDeck: DeckId | null): void {
	if (SETUP_ONLY_TYPES.has(command.type as string)) {
		throw new TypeError(`${where}: ${command.type} is setup and belongs in prepare, not a timed event`);
	}
	if (command.deck === undefined) return;
	if (command.deck !== clockDeck && command.deck !== incomingDeck) {
		throw new TypeError(`${where}: a timed command may touch only the clock or incoming deck`);
	}
}

function _event(value: unknown, index: number, clockDeck: DeckId, incomingDeck: DeckId | null, bars: number): PlanEvent {
	const where = `plan.events[${index}]`;
	const event = _record(value, where);
	const atBar = _finite(event.at_bar, `${where}.at_bar`);
	// A plan runs `bars` bars on the clock deck; a step outside that (a stop at bar 100 of an 8-bar plan) would
	// fire long after the plan is over. One bar before the window is the furthest a launch leads.
	if (atBar < -1 || atBar > bars) throw new RangeError(`${where}.at_bar is outside the plan's ${bars} bars`);
	if ('command' in event === 'ramp' in event) throw new TypeError(`${where} must carry exactly one of command or ramp`);
	if ('command' in event) {
		const command = _planCommand(event.command, `${where}.command`);
		_planDeckCommand(command, `${where}.command`, clockDeck, incomingDeck);
		return { at_bar: atBar, command };
	}
	const ramp = _record(event.ramp, `${where}.ramp`);
	const command = _planCommand(ramp.command, `${where}.ramp.command`);
	_planDeckCommand(command, `${where}.ramp.command`, clockDeck, incomingDeck);
	if (!PLAN_RAMP_TYPES.has(command.type as string)) {
		throw new TypeError(`${where}.ramp.command.type must be one of ${[...PLAN_RAMP_TYPES].join(', ')}`);
	}
	const to = _finite(ramp.to, `${where}.ramp.to`);
	if (command.type === 'tempo' ? to <= 0 || to > 4 : to < 0 || to > 1) {
		throw new RangeError(`${where}.ramp.to is out of range for ${String(command.type)}`);
	}
	const overBars = _finite(ramp.over_bars, `${where}.ramp.over_bars`);
	if (overBars <= 0) throw new RangeError(`${where}.ramp.over_bars must be > 0`);
	if (atBar + overBars > bars) throw new RangeError(`${where}.ramp runs past the plan's ${bars} bars`);
	return { at_bar: atBar, ramp: { command, to, over_bars: overBars } };
}

/** Validate the wire shape `apps/autodj/compile.py` emits. */
export function parsePlanOrder(payload: unknown): AgentPlanOrder {
	const plan = _record(payload, 'plan');
	if (typeof plan.id !== 'string' || plan.id === '') throw new TypeError('plan.id must be a non-empty string');
	const clock = _record(plan.clock, 'plan.clock');
	// The compiler always names the incoming deck; a raw plan without one would leave the room guards with no incoming side.
	if (plan.incoming === undefined || plan.incoming === null) throw new TypeError('plan.incoming must name the incoming deck');
	const incoming = _record(plan.incoming, 'plan.incoming');
	const clockDeck = _deck(clock.deck, 'plan.clock.deck');
	const incomingDeck = incoming === null ? null : _deck(incoming.deck, 'plan.incoming.deck');
	const startBar = _finite(plan.start_bar, 'plan.start_bar');
	if (!Number.isInteger(startBar) || startBar < 1) throw new RangeError('plan.start_bar must be a whole bar >= 1');
	const bars = _finite(plan.bars, 'plan.bars');
	if (bars <= 0) throw new RangeError('plan.bars must be > 0');
	if (!Array.isArray(plan.prepare)) throw new TypeError('plan.prepare must be a list');
	if (!Array.isArray(plan.events) || plan.events.length === 0) throw new TypeError('plan.events must be a non-empty list');
	// Sol P1 #7595 (ADR-NEW-plan-order-notes): compile always emits the notes, so they are required: an order without them is refused.
	const notes = plan.notes;
	if (!Array.isArray(notes) || !notes.every((note) => typeof note === 'string')) throw new TypeError('plan.notes must be a list of strings');
	return {
		id: plan.id,
		clock: { deck: clockDeck, stable_id: _stableId(clock.stable_id, 'plan.clock.stable_id') },
		incoming:
			incoming === null || incomingDeck === null
				? null
				: { deck: incomingDeck, stable_id: _stableId(incoming.stable_id, 'plan.incoming.stable_id') },
		start_bar: startBar,
		bars,
		style: typeof plan.style === 'string' ? plan.style : null,
		muted: plan.muted === true,
		prepare: plan.prepare.map((command, index) => _prepareCommand(command, `plan.prepare[${index}]`, clockDeck, incomingDeck)),
		events: plan.events.map((event, index) => _event(event, index, clockDeck, incomingDeck, bars)).sort((a, b) => a.at_bar - b.at_bar),
		notes: [...notes]
	};
}

// --------------------------------------------------------------- execute ---

function _rampKey(command: Record<string, unknown>): string {
	return [command.type, command.deck ?? '', command.band ?? ''].join(':');
}

function _currentValue(state: PlanStateView, command: Record<string, unknown>): number {
	if (command.type === 'crossfader') return state.mixer.crossfader;
	const deck = command.deck as DeckId;
	if (command.type === 'tempo') return state.decks[deck].pitch;
	const channel = state.mixer.channels[deck];
	const key = command.type === 'eq' ? `eq_${String(command.band)}` : String(command.type);
	const value = (channel as Record<string, unknown>)[key];
	if (typeof value !== 'number' || !Number.isFinite(value)) throw new Error(`no current value for ${key} on deck ${deck}`);
	return value;
}

/** The same play without its quantize flag, so it starts at once; any other command as is. */
function _immediate(command: PlanCommand): PlanCommand {
	if (command.type !== 'play' || command.quantize === undefined) return command;
	const immediate = { ...command };
	delete immediate.quantize;
	return immediate;
}

function _withValue(command: PlanCommand, value: number): PlanCommand {
	return command.type === 'tempo' ? { ...command, ratio: value } : { ...command, value };
}

interface ActiveRamp {
	command: Record<string, unknown>;
	from: number;
	to: number;
	fromBar: number;
	overBars: number;
}

export interface PlanRunner {
	schedule: (payload: unknown) => Promise<{ plan_id: string; status: AgentPlanStatus; bar: number }>;
	amend: (payload: unknown) => Promise<{ plan_id: string; status: AgentPlanStatus; bars: number }>;
	mirror: () => AgentPlanMirror | null;
	/** Run one tick now (tests drive the clock with this). */
	tick: () => Promise<void>;
	dispose: () => Promise<void>;
}

/** Each runner's step ids carry its own serial, so a runner recreated in the same engine session (a remount, a retried
 * plan id) never reuses an earlier runner's ids (Codex + Sol #7594). */
let _runnerSerial = 0;

export function createPlanRunner(deps: PlanDeps): PlanRunner {
	let order: AgentPlanOrder | null = null;
	let status: AgentPlanStatus = 'done';
	let reason: string | null = null;
	let fired: boolean[] = [];
	let bar: number | null = null;
	let endedAt: number | null = null;
	let timer: unknown = null;
	let ticking = false;
	/** An amendment transaction is running; a tick arriving meanwhile is queued in `tickQueued` and run after it. */
	let amending = false;
	let tickQueued = false;
	let inFlight: Promise<void> = Promise.resolve();
	/** Set by dispose: the page that owned this runner is gone, so nothing may dispatch again. */
	let disposed = false;
	let disposing: Promise<void> | null = null;
	/**
	 * Set by dispose before it waits for the tick and dispatch in flight: no new tick starts, and their remaining steps are
	 * dropped. Only the launch cancel that dispose sends after the wait goes out (`cancelling`).
	 */
	let closing = false;
	let cancelling = false;

	/**
	 * Ends a live plan and cancels its armed launch. The tick and dispatch in flight settle first: either may still arm a
	 * launch the cancel must reach. The cancel is sent before `disposed` stops every later dispatch.
	 */
	async function _closeForDispose(): Promise<void> {
		closing = true;
		if (timer !== null) deps.clearTimer(timer);
		timer = null;
		await inFlight.catch(() => undefined);
		await lastDispatch.catch(() => undefined);
		cancelling = true;
		try {
			if (active()) await _end('aborted', 'performance page closed');
			// No later plan will come to revoke what is still armed, so the page's close tries once more itself.
			await _revokeLeftoverSteps();
		} finally {
			cancelling = false;
			disposed = true;
		}
	}
	const ramps = new Map<string, ActiveRamp>();
	/** AGENT-30: steps sent ahead on the audio clock that may not have landed yet, with the value each replaces. */
	let scheduled: { id: string; command: PlanCommand; at: number; previous: number; index: number }[] = [];
	let stepSeq = 0;
	const runnerSerial = (_runnerSerial += 1);

	const active = (): boolean => order !== null && (status === 'scheduled' || status === 'running');

	/** WAVEPLAN-7896b: what the waveform shows, and what it was built from, so a tick rebuilds only after a real move. */
	let overlay: PlanOverlay | null = null;
	let overlayOrder: AgentPlanOrder | null = null;
	let overlayAlignment: IncomingAlignment | null = null;
	let overlayClockGrid: object | null = null;
	let overlayIncomingGrid: object | null = null;
	let overlayChannels: Record<number, object> = {};
	let overlayLiveKnobs = false;
	/** The build failure already reported, so a retry each tick does not republish it. */
	let overlayFailure: string | null = null;

	function _channelSnapshot(state: PlanStateView): Record<number, object> {
		return Object.fromEntries(Object.entries(state.mixer.channels).map(([deck, channel]) => [deck, { ...channel }]));
	}

	/** Whether a knob the curves start from moved since the snapshot: a ramp runs from the value it finds when it fires. */
	function _knobsMoved(state: PlanStateView, plan: AgentPlanOrder): boolean {
		for (const deck of plan.incoming === null ? [plan.clock.deck] : [plan.clock.deck, plan.incoming.deck]) {
			const live = state.mixer.channels[deck] as Record<string, unknown>;
			const drawn = overlayChannels[deck] as Record<string, unknown>;
			for (const knob of OVERLAY_KNOBS) if (live[knob] !== drawn[knob]) return true;
		}
		return false;
	}

	function _publishOverlay(next: PlanOverlay | null): void {
		overlay = next;
		deps.onOverlay?.(next);
	}

	function _overlayInput(state: PlanStateView, plan: AgentPlanOrder): PlanOverlayInput {
		const clock = state.decks[plan.clock.deck];
		const incoming = plan.incoming === null ? null : state.decks[plan.incoming.deck];
		return {
			order: plan,
			channels: overlayChannels,
			liveKnobs: overlayLiveKnobs,
			clock: { stable_id: clock.stable_id, position_ms: clock.position_ms, downbeats: _downbeatsOf(clock) },
			incoming:
				incoming === null
					? null
					: { stable_id: incoming.stable_id, position_ms: incoming.position_ms, downbeats: _downbeatsOf(incoming), playing: incoming.playing },
			gridBarsPerClockBar: incoming === null ? null : _gridBarsPerClockBar(clock, incoming)
		};
	}

	/** Runs on every tick. The overlay only ever describes the plan, so its failure is logged and never reaches the transport. */
	function _refreshOverlay(): void {
		if (!active()) return;
		const plan = order!;
		try {
			const state = deps.query();
			const input = _overlayInput(state, plan);
			const alignment = incomingAlignment(input);
			// A grid that lands or changes late moves the curves without moving the incoming deck against the plan.
			const clockGrid = state.decks[plan.clock.deck].beatgrid;
			const incomingGrid = plan.incoming === null ? null : state.decks[plan.incoming.deck].beatgrid;
			const sameGrids = clockGrid === overlayClockGrid && incomingGrid === overlayIncomingGrid;
			// Only before the first event: past it, the plan itself is moving these knobs, and the curves are its own.
			const knobsMoved = !fired.some(Boolean) && _knobsMoved(state, plan);
			if (plan === overlayOrder && sameGrids && !knobsMoved && !alignmentChanged(overlayAlignment, alignment)) return;
			if (knobsMoved) {
				overlayChannels = _channelSnapshot(state);
				overlayLiveKnobs = true;
				input.channels = overlayChannels;
				input.liveKnobs = true;
			}
			overlayOrder = plan;
			overlayAlignment = alignment;
			overlayClockGrid = clockGrid;
			overlayIncomingGrid = incomingGrid;
			_publishOverlay(buildPlanOverlay(input, alignment));
			overlayFailure = null;
		} catch (error) {
			// Forgetting what was drawn makes the next tick try again.
			overlayOrder = null;
			const message = error instanceof Error ? error.message : String(error);
			if (message === overlayFailure) return;
			overlayFailure = message;
			console.error('plan overlay', error);
			_publishOverlay(failedPlanOverlay(plan));
		}
	}

	function _stop(next: AgentPlanStatus, why: string | null): void {
		if (disposed) return;
		overlayOrder = null;
		overlayAlignment = null;
		overlayFailure = null;
		if (overlay !== null) _publishOverlay(null);
		status = next;
		reason = why;
		endedAt = deps.now();
		ramps.clear();
		if (timer !== null) deps.clearTimer(timer);
		timer = null;
	}

	/** Every dispatch goes through here, so a tick still in flight at dispose sends nothing more. */
	/** The last dispatch to settle, so `extend` can wait for the command in flight before it reads the plan. */
	let lastDispatch: Promise<void> = Promise.resolve();

	/** Dispatch, answering whether the command was sent: a tick still in flight at dispose sends nothing, and a revoke
	 * that was not sent must not count as done (blind review P1 #7594), so its step stays for dispose's own revoke. */
	async function _dispatch(command: PlanCommand): Promise<boolean> {
		if (disposed) throw new Error('plan runner disposed: the performance page closed');
		if (closing && !cancelling) return false;
		const sent = deps.dispatch(command);
		lastDispatch = sent.then(
			() => undefined,
			() => undefined
		);
		await sent;
		return true;
	}

	/**
	 * Whether the incoming deck is committed to the plan: its latest fired transport command is a play. Events
	 * fire in index order, so the last fired play or stop decides. An authored stop releases the deck, and a
	 * later authored play commits it again.
	 */
	function _incomingPlayFired(): boolean {
		const plan = order!;
		if (plan.incoming === null) return false;
		const incoming = plan.incoming.deck;
		let committed = false;
		plan.events.forEach((event, index) => {
			if (!fired[index] || !('command' in event)) return;
			const { command } = event;
			if (command.type === 'play' && command.deck === incoming) committed = command.playing === true;
		});
		return committed;
	}

	/**
	 * Whether a clock stop must wait: the committed incoming deck is armed for its downbeat but not playing yet.
	 * The outgoing deck stays the room until the launch lands, so the stop stays unfired and the next pass retries it.
	 */
	function _clockStopAwaitsLaunch(command: PlanCommand): boolean {
		const plan = order!;
		if (plan.incoming === null || command.type !== 'play' || command.deck !== plan.clock.deck) return false;
		if (command.playing !== false) return false;
		return _armedOnly(deps.query().decks[plan.incoming.deck]);
	}

	/**
	 * Whether a mixer close of the outgoing (clock) deck waits for the committed incoming deck's launch. The incoming
	 * deck is armed for its downbeat but not playing, so its open channel is no path yet: a close now would leave the
	 * room silent until the launch lands. Like the outgoing stop, the close stays unfired and the next pass retries it.
	 */
	function _closeAwaitsLaunch(command: PlanCommand, value: number | null = typeof command.value === 'number' ? command.value : null): boolean {
		const plan = order!;
		if (plan.incoming === null) return false;
		const incoming = deps.query().decks[plan.incoming.deck];
		if (!_armedOnly(incoming)) return false;
		// AGENT-30: a close scheduled on the audio clock at or after the launch's own start lands with it, never before.
		const launchAt = incoming.quantized_launch_armed?.launch_at_context_sec;
		if (Number.isFinite(command.at_context_sec) && Number.isFinite(launchAt) && (command.at_context_sec as number) >= (launchAt as number)) return false;
		return _outgoingClosedBy(command, value);
	}

	/** Whether a mixer move would leave the playing outgoing (clock) deck with its path closed, read on the current mixer. */
	function _outgoingClosedBy(command: PlanCommand, value: number | null): boolean {
		if (value === null) return false;
		const state = deps.query();
		const clock = order!.clock.deck;
		let crossfader = state.mixer.crossfader;
		let clockChannel = state.mixer.channels[clock] as Record<string, unknown>;
		if (command.type === 'crossfader') crossfader = value;
		else if ((command.type === 'fader' || command.type === 'trim') && command.deck === clock) {
			clockChannel = { ...clockChannel, [command.type]: value };
		} else return false;
		return state.decks[clock].playing === true && (_channelSilent(clockChannel, crossfader, state.mixer.master) || deps.stemsSilent(clock));
	}

	/**
	 * Why a command must not dispatch, or null: it would leave the room with both paths closed, the outgoing (clock)
	 * deck and the committed incoming deck. A stop of the outgoing deck needs the incoming deck audible; a stop of the
	 * incoming deck needs the outgoing deck still heard, since the room then rests on it. A mixer move (a timed set, or
	 * one ramp step at `value`) is projected onto both channels and the crossfader first, and the incoming side counts
	 * as closed when its fader, trim, crossfader or master is shut, or its transport has stopped. Reads are fresh,
	 * since an earlier dispatch in the same tick can pause the incoming deck.
	 */
	/**
	 * The mirror as the room will be once every step already sent ahead has landed (Codex BLOCKING #7594). The bus
	 * mirrors a scheduled step only when it lands, so a guard reading the bare mirror would judge a later step against a
	 * room that an earlier, still pending step changes first: a close at the same bar as a pending open read as silence.
	 */
	/**
	 * The steps sent ahead that are still the plan's AND still waiting to land: the one predicate every reader of
	 * `scheduled` uses (projection, the same-control guard, completion, extend, revoke). It asks the engine, which owns
	 * each step's state (Sol P1 #7594); nothing is inferred from the mirror's value or from elapsed time, so a mirror
	 * write delayed by a stalled main thread still reads waiting, and an operator's plain set reads cancelled at once.
	 */
	/** Replace the tracked steps; every step that leaves the list is forgotten by the engine (a waiting one never is). */
	function _setScheduled(next: typeof scheduled): void {
		const kept = new Set(next);
		for (const step of scheduled) if (!kept.has(step)) deps.forgetStep(step.id);
		scheduled = next;
	}

	function _waiting(): typeof scheduled {
		return scheduled.filter((step) => deps.stepState(step.id) === 'waiting');
	}

	/**
	 * The room once every step still waiting has landed. With `atBar` (Codex #7594), the room at that plan bar: only
	 * the waiting steps due by then, and every room ramp that finishes by then (running or not yet fired) at its
	 * target, so a command is judged against the room it will actually meet.
	 */
	function _projected(atBar: number | null = null, withRamps = true): PlanStateView {
		const state = deps.query();
		const settled = atBar === null || !withRamps ? [] : _roomRampsDoneBy(atBar);
		const due = atBar === null ? _waiting() : _waiting().filter((step) => order!.events[step.index].at_bar <= atBar);
		const pending = [...due, ...settled.map((command) => ({ command }))];
		if (pending.length === 0) return state;
		const channels = { ...state.mixer.channels } as Record<string, Record<string, unknown>>;
		let crossfader = state.mixer.crossfader;
		for (const { command } of pending) {
			if (typeof command.value !== 'number') continue;
			if (command.type === 'crossfader') crossfader = command.value;
			else if (command.deck !== undefined) {
				const key = command.type === 'eq' ? `eq_${String(command.band)}` : String(command.type);
				channels[command.deck as DeckId] = { ...channels[command.deck as DeckId], [key]: command.value };
			}
		}
		return { ...state, mixer: { ...state.mixer, crossfader, channels } } as PlanStateView;
	}

	/** Each room ramp (fader, trim, crossfader) that has finished by plan bar `atBar`, as the set it ends on. */
	function _roomRampsDoneBy(atBar: number): PlanCommand[] {
		const room = (command: Record<string, unknown>) => ROOM_RAMP_TYPES.has(command.type as string);
		const running = [...ramps.values()].filter((ramp) => room(ramp.command) && ramp.fromBar + ramp.overBars <= atBar);
		const unfired = order!.events.flatMap((other, index) =>
			!fired[index] && 'ramp' in other && room(other.ramp.command) && other.at_bar + other.ramp.over_bars <= atBar ? [other] : []
		);
		return [
			...running.map((ramp) => ({ ...ramp.command, value: ramp.to }) as PlanCommand),
			...unfired.map((other) => ({ ...(other as { ramp: { command: PlanCommand; to: number } }).ramp.command, value: (other as { ramp: { to: number } }).ramp.to }) as PlanCommand)
		];
	}

	function _refusal(
		command: PlanCommand,
		value: number | null = typeof command.value === 'number' ? command.value : null,
		atBar: number | null = null,
		withRamps = true
	): string | null {
		const plan = order!;
		if (command.type === 'play') {
			const stopsClock = command.deck === plan.clock.deck && command.playing === false;
			const stopsIncoming = command.deck === plan.incoming?.deck && command.playing === false;
			const state = _projected();
			// A quantized launch aligns to the master: after prepare, a master change would phase the incoming track to another deck.
			const launches = command.deck === plan.incoming?.deck && command.playing === true && command.quantize === true;
			if (launches && state.master_deck !== plan.clock.deck) {
				return `clock deck ${plan.clock.deck} is no longer master, so the incoming launch would not align to it`;
			}
			if (!stopsClock && !stopsIncoming) return null;
			if (stopsIncoming) return _outgoingSilenced(state);
			return _incomingSilenced(state) ?? _incomingStopped(state);
		}
		if (value === null || plan.incoming === null) return null;
		const state = _projected(atBar, withRamps);
		const clock = plan.clock.deck;
		const incomingDeck = plan.incoming.deck;
		let crossfader = state.mixer.crossfader;
		let clockChannel = state.mixer.channels[clock] as Record<string, unknown>;
		let incomingChannel = state.mixer.channels[incomingDeck] as Record<string, unknown>;
		if (command.type === 'crossfader') crossfader = value;
		else if ((command.type === 'fader' || command.type === 'trim') && command.deck === clock) {
			clockChannel = { ...clockChannel, [command.type]: value };
		} else if ((command.type === 'fader' || command.type === 'trim') && command.deck === incomingDeck) {
			incomingChannel = { ...incomingChannel, [command.type]: value };
		} else return null;
		// A paused outgoing deck is no path at all, so the room then rests on the incoming side alone.
		const clockPlaying = state.decks[clock].playing === true;
		if (clockPlaying && !_channelSilent(clockChannel, crossfader, state.mixer.master) && !deps.stemsSilent(clock)) return null;
		const incomingStopped = _incomingPlayFired() ? _incomingStopped(state) : _incomingNotStarted();
		if (incomingStopped === null && !_channelSilent(incomingChannel, crossfader, state.mixer.master) && !deps.stemsSilent(incomingDeck)) {
			return null;
		}
		const incoming = incomingStopped ?? `incoming deck ${incomingDeck} is silent (fader, trim, crossfader, master or every stem part closed)`;
		const outgoing = clockPlaying ? `outgoing deck ${clock} would go silent` : `outgoing deck ${clock} is not playing`;
		return `${outgoing}: ${incoming}`;
	}

	/** Why the outgoing (clock) deck would leave the room silent on its own, or null while it is heard. */
	function _outgoingSilenced(state: PlanStateView): string | null {
		const clock = order!.clock.deck;
		if (state.decks[clock].playing !== true) return `outgoing deck ${clock} is not playing`;
		const channel = state.mixer.channels[clock] as Record<string, unknown>;
		if (!_channelSilent(channel, state.mixer.crossfader, state.mixer.master) && !deps.stemsSilent(clock)) return null;
		return `outgoing deck ${clock} is silent (fader, trim, crossfader, master or every stem part closed)`;
	}

	/**
	 * Why the committed incoming deck would play into a closed path, or null. The handover stop of the outgoing
	 * deck runs only while the incoming deck is audible; mixer controls stay live during a plan, so this is read
	 * before the stop, not once at order time.
	 */
	function _incomingSilenced(state: PlanStateView): string | null {
		const plan = order!;
		if (plan.incoming === null) return null;
		if (!_incomingPlayFired()) return _incomingNotStarted();
		const deck = plan.incoming.deck;
		const channel = state.mixer.channels[deck] as Record<string, unknown>;
		if (!_channelSilent(channel, state.mixer.crossfader, state.mixer.master) && !deps.stemsSilent(deck)) return null;
		return `incoming deck ${deck} is silent (fader, trim, crossfader, master or every stem part closed)`;
	}

	/** Why the committed incoming deck has not started yet: until its play fires, the outgoing deck is the whole room. */
	function _incomingNotStarted(): string {
		return `incoming deck ${order!.incoming!.deck} has not started yet`;
	}

	/**
	 * Why the incoming deck started or armed before the plan's own play fired, or null. The prepare-time rechecks end
	 * with schedule, so the operator can still start the deck before the play's tick; that deck would then enter early
	 * and the transition would run against playback that did not begin on the plan's downbeat.
	 */
	function _mutedPlayRefusal(command: PlanCommand, state: PlanStateView): string | null {
		if (order!.muted !== true || command.type !== 'play' || command.playing !== true) return null;
		if (state.master?.muted === true) return null;
		return `master is unmuted, so the play of deck ${String(command.deck)} would sound in a muted plan`;
	}

	function _incomingEarlyStart(state: PlanStateView): string | null {
		const plan = order!;
		if (plan.incoming === null || _incomingPlayFired()) return null;
		const incoming = state.decks[plan.incoming.deck];
		if (!incoming.playing && !_armedOnly(incoming)) return null;
		return `incoming deck ${plan.incoming.deck} started before the plan's play`;
	}

	/**
	 * Why the committed incoming deck is neither playing nor armed for its downbeat, or null while it is.
	 * Once its play has fired, the handover that stops the outgoing deck relies on it; a plan never stops its
	 * incoming deck, so a pause here is a failure, whether a tick or a finish_now sees it first.
	 */
	function _incomingStopped(state: PlanStateView): string | null {
		const plan = order!;
		if (plan.incoming === null || !_incomingPlayFired()) return null;
		const incoming = state.decks[plan.incoming.deck];
		if (incoming.playing || _armedOnly(incoming)) return null;
		return `incoming deck ${plan.incoming.deck} stopped mid-plan`;
	}

	/**
	 * Why the plan's transport is no longer its own at plan bar `at`, or null: the incoming deck was started early or
	 * stopped, or the clock deck stopped before its last bar (a stop there completes the handover in the tick).
	 * Checked before each awaited step, since an operator change during an earlier dispatch lands before the next one.
	 */
	function _transportLost(state: PlanStateView, at: number): string | null {
		const plan = order!;
		const clock = state.decks[plan.clock.deck];
		const clockStopped = clock.playing || at >= plan.bars - 1 ? null : `clock deck ${plan.clock.deck} stopped at plan bar ${at.toFixed(2)}`;
		return _incomingEarlyStart(state) ?? _incomingStopped(state) ?? clockStopped;
	}

	/** End the plan and cancel an incoming launch it committed, so nothing starts after it ends. */
	async function _end(next: 'failed' | 'aborted', why: string): Promise<void> {
		_stop(next, why);
		try {
			await _settleScheduled('revoke');
		} catch (error) {
			reason = `${why}; a step scheduled on the audio clock could not be revoked: ${error instanceof Error ? error.message : String(error)}`;
		}
		const incoming = order!.incoming;
		// The launch may still be in flight (armed only once its dispatch lands), so cancel on the fired play, not the mirror's armed flag.
		// An operator who replaced the incoming track owns its launch now, so the cancel is sent only while the plan's track is still there.
		if (incoming === null || !_incomingPlayFired() || deps.query().decks[incoming.deck].playing) return;
		if (deps.query().decks[incoming.deck].stable_id !== incoming.stable_id) return;
		try {
			// An unquantized stop clears the armed launch; the deck is not playing yet.
			await _dispatch({ type: 'play', deck: incoming.deck, playing: false });
		} catch (error) {
			reason = `${why}; the armed launch on deck ${incoming.deck} could not be cancelled: ${
				error instanceof Error ? error.message : String(error)
			}`;
		}
	}

	/**
	 * AGENT-30: settle every step still waiting for its audio-clock time with a plain set now (the bus cancels later
	 * automation on that param): `revoke` puts back the value it replaced (the plan ended), `apply` lands it at once
	 * (finish_now). One path for both, so nothing moves after the plan ends.
	 */
	async function _settleScheduled(mode: 'revoke' | 'apply'): Promise<void> {
		// Only steps the engine still holds as waiting: one an operator's plain set cancelled is no longer the plan's, and
		// one that landed is the room's. One step per control waits at a time.
		const waiting = _waiting();
		_setScheduled([]);
		// Revoke walks newest first, so two steps on one control unwind to the value before both; apply walks in plan
		// order, so the later step wins. Every step is attempted; failures are raised together after the walk.
		const walk = mode === 'revoke' ? [...waiting].reverse() : waiting;
		const errors: string[] = [];
		// Sol P1 #7594: a step whose plain set was refused still has its automation armed on the audio clock, so it stays
		// in the list: the failure that follows can then revoke it rather than let it land after the plan ended.
		const unsettled: typeof scheduled = [];
		for (const step of walk) {
			const plain: PlanCommand = { ...step.command, value: mode === 'revoke' ? step.previous : step.command.value };
			delete plain.at_context_sec;
			delete plain.step_id;
			try {
				if (!(await _dispatch(plain))) {
					unsettled.push(step); // not sent (dispose holds the bus): still armed, so dispose revokes it
					continue;
				}
			} catch (error) {
				unsettled.push(step);
				errors.push(`${_rampKey(step.command)}: ${error instanceof Error ? error.message : String(error)}`);
			}
		}
		// The settled steps are finished now (cancelled by their plain set), so the engine may drop them too.
		for (const step of walk) if (!unsettled.includes(step)) deps.forgetStep(step.id);
		_setScheduled([...unsettled.sort((a, b) => a.index - b.index), ...scheduled]);
		if (errors.length > 0) throw new Error(`${errors.length} scheduled step(s) could not be settled: ${errors.join('; ')}`);
	}

	/** Revoke the steps an ended plan still has waiting (its own revoke was refused); a refusal is raised as plan_unsettled. */
	async function _revokeLeftoverSteps(): Promise<void> {
		if (_waiting().length === 0) return;
		try {
			await _settleScheduled('revoke');
		} catch (error) {
			throw new Error(`plan_unsettled: a step of the previous plan could not be revoked: ${error instanceof Error ? error.message : String(error)}`);
		}
	}

	function _arm(): void {
		if (disposed || !active()) return;
		timer = deps.setTimer(() => {
			timer = null;
			void tick().finally(_arm);
		}, PLAN_TICK_MS);
	}

	async function _setCommand(command: PlanCommand): Promise<void> {
		ramps.delete(_rampKey(command));
		await _dispatch(command);
	}

	/** A read a transition needs, or undefined after failing the plan with why it could not be read. */
	async function _readOrFail(read: () => number): Promise<number | undefined> {
		try {
			return read();
		} catch (error) {
			await _end('failed', error instanceof Error ? error.message : String(error));
			return undefined;
		}
	}

	async function _fire(index: number, atBar: number, atContextSec: number | null = null): Promise<void> {
		const source = order!.events[index];
		const event: PlanEvent =
			atContextSec !== null && 'command' in source
				? { ...source, command: { ...source.command, at_context_sec: atContextSec, step_id: `${order!.id}:r${runnerSerial}:${index}:${(stepSeq += 1)}` } }
				: source;
		// Read live before each event: an earlier event's awaited dispatch in this tick can let the operator start the incoming deck.
		const early = _incomingEarlyStart(deps.query());
		if (early !== null) {
			await _end('failed', early);
			return;
		}
		if ('command' in event) {
			if (_clockStopAwaitsLaunch(event.command)) return;
			if (_closeAwaitsLaunch(event.command)) return;
			// A refused command stays unfired: the committed transport is then what it was before it, so the
			// cancel in _end still sees an armed incoming launch and clears it.
			// The transport check follows the command's own refusal, so a refusal that names the command keeps its message.
			const refused = _refusal(event.command, undefined, event.at_bar) ?? _tempoSetRefusal(event.command) ?? _transportLost(deps.query(), atBar);
			if (refused !== null) {
				await _end('failed', refused);
				return;
			}
			// AGENT-25: the operator may have unmuted the master since schedule, and a muted plan's play would then sound.
			const unmuted = _mutedPlayRefusal(event.command, deps.query());
			if (unmuted !== null) {
				await _end('failed', unmuted);
				return;
			}
			// Sol P1 #7594 (the class: read what a transition needs before mutating): the value a sent-ahead step replaces is
			// read BEFORE the event is marked fired, and a mirror without it fails the plan rather than leaving it fired.
			const previous = atContextSec === null ? null : await _readOrFail(() => _currentValue(deps.query(), event.command));
			if (previous === undefined) return;
			fired[index] = true;
			try {
				await _setCommand(event.command);
				if (previous !== null && atContextSec !== null) {
					scheduled.push({ id: String(event.command.step_id), command: event.command, at: atContextSec, previous, index });
				}
			} catch (error) {
				// A rejected dispatch never reached the bus, so it is unfired before the failure ends the plan.
				fired[index] = false;
				throw error;
			}
			return;
		}
		// A tempo target the operator's narrowed pitch range no longer allows fails before the ramp registers.
		const outside = _tempoRangeRefusal(event.ramp.command, event.ramp.to);
		if (outside !== null) {
			await _end('failed', outside);
			return;
		}
		const { command, to, over_bars } = event.ramp;
		// Read live, not the tick's snapshot: a set on this control earlier in the same tick has already moved it, and
		// the ramp continues from there. Read before the event is marked fired, as for a set.
		const from = await _readOrFail(() => _currentValue(_projected(), command));
		if (from === undefined) return;
		fired[index] = true;
		ramps.set(_rampKey(command), {
			command,
			from,
			to,
			fromBar: atBar,
			overBars: over_bars
		});
	}

	/**
	 * Why the plan no longer owns its decks, or null while it does: a deck holds another track, or the operator moved
	 * the master off the clock deck. The launch and the outgoing stop are both aligned to the master, so it is checked
	 * with the tracks before every awaited step, until the plan's own master event begins the handover.
	 */
	function _lostTrack(state: PlanStateView): string | null {
		const plan = order!;
		if (state.decks[plan.clock.deck].stable_id !== plan.clock.stable_id) {
			return `clock deck ${plan.clock.deck} no longer holds ${plan.clock.stable_id}`;
		}
		if (plan.incoming !== null && state.decks[plan.incoming.deck].stable_id !== plan.incoming.stable_id) {
			return `incoming deck ${plan.incoming.deck} no longer holds ${plan.incoming.stable_id}`;
		}
		return _masterLost(state);
	}

	/**
	 * Why the master left its deck, or null: before the plan's own master event it stays on the clock deck, and after it
	 * on the incoming deck. An operator who elects a third deck during the handover's awaited master dispatch is refused.
	 */
	function _masterLost(state: PlanStateView): string | null {
		const plan = order!;
		// Only the fired master event that promotes the incoming deck is the handover: a redundant master on the clock deck is not.
		const handedOver = plan.incoming !== null && plan.events.some((event, index) => fired[index] && 'command' in event && event.command.type === 'master' && event.command.deck === plan.incoming!.deck);
		// The engine's automatic level handoff may elect the incoming deck once the outgoing path closes, before the plan's own
		// master event fires: that election is the handover too. Any other deck is still a lost master.
		const electedIncoming = plan.incoming !== null && state.master_deck === plan.incoming.deck;
		const owner = handedOver || electedIncoming ? plan.incoming!.deck : plan.clock.deck;
		if (state.master_deck === owner) return null;
		return handedOver
			? `master moved to deck ${String(state.master_deck)} off incoming deck ${owner} after the handover`
			: `master moved to deck ${String(state.master_deck)} off clock deck ${plan.clock.deck} before the handover`;
	}

	/** Why the clock deck stopped mid-plan, or null. A clock that runs out in the last bar is the handover itself. */
	function _clockStopped(state: PlanStateView): string | null {
		const plan = order!;
		const handedOff = plan.events.some((event, index) => fired[index] && 'command' in event && event.command.type === 'play' && event.command.deck === plan.clock.deck && event.command.playing === false);
		return !handedOff && (bar ?? 0) < plan.bars - 1 && !state.decks[plan.clock.deck].playing ? `clock deck ${plan.clock.deck} stopped mid-finish` : null;
	}

	/**
	 * Why the transport is no longer the plan's during finish_now, or null: the incoming deck was started early or
	 * stopped, or the clock deck stopped before the plan's own handover stop, which finish_now sends itself.
	 */
	function _finishTransportLost(state: PlanStateView): string | null {
		return _incomingEarlyStart(state) ?? _incomingStopped(state) ?? _clockStopped(state);
	}

	/** Start an armed incoming deck now: an unquantized play first disarms it, the next one plays. Each attempt rechecks ownership and the mute. */
	async function _startArmedNow(deck: DeckId): Promise<void> {
		for (let attempt = 0; attempt < 2 && !deps.query().decks[deck].playing; attempt += 1) {
			// The clock is rechecked, not the incoming deck: the helper's own first play disarms it, which is not an operator stop.
			const refused = _lostTrack(deps.query()) ?? _clockStopped(deps.query()) ?? _mutedPlayRefusal({ type: 'play', deck, playing: true }, deps.query());
			if (refused !== null) {
				await _end('failed', refused);
				throw new Error(`plan_failed: ${refused}`);
			}
			await _dispatch({ type: 'play', deck, playing: true });
		}
		if (deps.query().decks[deck].playing) return;
		const why = `incoming deck ${deck} did not start for finish_now`;
		await _end('failed', why);
		throw new Error(`plan_failed: ${why}`);
	}

	async function _finishNow(): Promise<void> {
		// A quantized play that already fired is waiting for its downbeat, and the
		// handover stops the outgoing deck right after: start the incoming deck before
		// any outgoing fall settles, so the room never rests on neither path.
		const incoming = order!.incoming;
		// An incoming deck the operator started or armed before the plan's own play fired is refused here too, as on each tick.
		const early = _incomingEarlyStart(deps.query());
		if (early !== null) {
			await _end('failed', early);
			throw new Error(`plan_failed: ${early}`);
		}
		if (incoming !== null && _armedOnly(deps.query().decks[incoming.deck])) {
			// AGENT-25: the launch below is a play, so a muted plan refuses it while the master is unmuted.
			const launchRefused = _lostTrack(deps.query()) ?? _mutedPlayRefusal({ type: 'play', deck: incoming.deck, playing: true }, deps.query());
			if (launchRefused !== null) {
				await _end('failed', launchRefused);
				throw new Error(`plan_failed: ${launchRefused}`);
			}
			await _startArmedNow(incoming.deck);
		}
		// AGENT-30: a step sent ahead lands now, not after the plan reports done, and only once the armed incoming deck is
		// playing (Codex BLOCKING #7594): a pending outgoing cut applied first would leave the room on neither path, and a
		// refused launch must still find it pending so the failure revokes it.
		await _settleScheduled('apply');
		// Active ramps precede every unfired event. Settle them first so later
		// authored sets (especially the handover's neutral strip reset) win.
		for (const ramp of [...ramps.values()]) {
			// A tempo target the operator's narrowed pitch range no longer allows fails before any step is sent.
			// Each step awaits a dispatch, and the operator may replace a plan track in that time: ownership is re-read before every step.
			const refused = _lostTrack(deps.query()) ?? _finishTransportLost(deps.query()) ?? _refusal(ramp.command, ramp.to) ?? _tempoRangeRefusal(ramp.command, ramp.to);
			if (refused !== null) {
				await _end('failed', refused);
				throw new Error(`plan_failed: ${refused}`);
			}
			await _setCommand(_withValue(ramp.command, ramp.to));
		}
		for (let index = 0; index < order!.events.length; index += 1) {
			if (fired[index]) continue;
			const event = order!.events[index];
			// A quantized play only arms the next downbeat, and the handover stops
			// the outgoing deck right after it: start the incoming deck now.
			if ('command' in event) {
				const refused = _lostTrack(deps.query()) ?? _finishTransportLost(deps.query()) ?? _refusal(event.command) ?? _tempoSetRefusal(event.command) ?? _mutedPlayRefusal(event.command, deps.query());
				if (refused !== null) {
					await _end('failed', refused);
					throw new Error(`plan_failed: ${refused}`);
				}
				fired[index] = true;
				try {
					await _setCommand(_immediate(event.command));
				} catch (error) {
					fired[index] = false;
					throw error;
				}
			}
			else {
				const refused = _lostTrack(deps.query()) ?? _finishTransportLost(deps.query()) ?? _refusal(event.ramp.command, event.ramp.to) ?? _tempoRangeRefusal(event.ramp.command, event.ramp.to);
				if (refused !== null) {
					await _end('failed', refused);
					throw new Error(`plan_failed: ${refused}`);
				}
				fired[index] = true;
				try {
					await _setCommand(_withValue(event.ramp.command, event.ramp.to));
				} catch (error) {
					fired[index] = false;
					throw error;
				}
			}
		}
		// The last dispatch is awaited too: a pause that lands during it must fail the plan, not complete it.
		const lostAtEnd = _lostTrack(deps.query()) ?? _finishTransportLost(deps.query());
		if (lostAtEnd !== null) {
			await _end('failed', lostAtEnd);
			throw new Error(`plan_failed: ${lostAtEnd}`);
		}
		_stop('done', reason);
	}

	/**
	 * AGENT-30: the AudioContext time of a step set's bar when that bar is within `STEP_LOOKAHEAD_S`, else null (not
	 * a step, or not due yet). The clock deck's playhead advances `pitch` track ms per real ms.
	 */
	/**
	 * Sol P1 #7594: a step sent ahead holds the AudioContext time its bar had on the clock deck's timeline when it was
	 * sent. A seek, tempo change, pause, loop or master-delay change before it lands moves that mapping, which the
	 * position anchor already models (it reads null once the timeline revision moves on). Every tick re-derives each
	 * waiting, not yet started step's time from the CURRENT anchor; a step whose anchor is gone or whose time moved is
	 * revoked to the value it replaced and marked unfired, so it is sent again on the new timeline or fires on its bar.
	 */
	/**
	 * Codex BLOCKING #7594: a graph rebuild ends the engine session, which drops every pending step's automation and
	 * its state (the engine reports null for it). Unlike a step an operator's plain set cancelled, such a step is still
	 * the plan's: it is unfired, so it is sent again on the new graph or fires on its bar, never silently skipped.
	 */
	function _unfireStepsLostWithTheEngine(): void {
		// Codex BLOCKING #7594: a landed or cancelled step is finished: it is retired here and never re-fired, whatever
		// happens to the engine later. Only a step the engine reports `lost` (or has never seen) is the plan's to retry.
		const states = new Map(scheduled.map((step) => [step, deps.stepState(step.id)] as const));
		const lost = scheduled.filter((step) => states.get(step) === 'lost' || states.get(step) === null);
		_setScheduled(scheduled.filter((step) => states.get(step) === 'waiting'));
		for (const step of lost) fired[step.index] = false;
	}

	/**
	 * Retire finished steps, then name every waiting step that must be revoked and judged again: the ones `stale`
	 * names, and (Codex #7594) every waiting fader step the silence guard would refuse now. Ticks and amendments both
	 * run this before they act on the list,
	 * and await the revoke only when it names a step (an await with nothing to do shifts a tick's timing).
	 */
	function _retireAndRejudge(stale: () => typeof scheduled): typeof scheduled {
		_unfireStepsLostWithTheEngine();
		// Codex #7594 (the class): a fader step sent ahead was judged against the room as it was then. Anything since
		// (an operator cancelling a pending opening, closing a trim or the crossfader path) may make it silence the room,
		// so each is judged again now; one the silence guard would now refuse goes back to its bar, where it fails as before.
		const rejudge = _waiting().filter(
			(step) => step.command.type === 'fader' && _refusal(step.command, undefined, order!.events[step.index].at_bar) !== null
		);
		return [...new Set([...stale(), ...rejudge])].sort((a, b) => a.index - b.index);
	}

	function _stepsOffTimeline(downbeats: readonly number[]): typeof scheduled {
		const now = deps.contextNowSec();
		const anchor = deps.positionAnchor(order!.clock.deck);
		const off = _waiting().filter((step) => {
			if (step.at <= now) return false; // already started on the audio clock: nothing left to move
			if (anchor === null) return true;
			const event = order!.events[step.index];
			const at = anchor.context_sec + (msAtBar(downbeats, order!.start_bar + event.at_bar) - anchor.position_ms) / anchor.pitch / 1000;
			return Math.abs(at - step.at) > TIMELINE_TOLERANCE_S;
		});
		return off;
	}

	async function _revokeOffTimeline(off: typeof scheduled): Promise<void> {
		for (const step of [...off].reverse()) {
			// Codex #7594 (the class): a step leaves the list only once its revoke went through, so a refused one is still
			// there for the failure's own revoke rather than left armed after the plan ended.
			const plain: PlanCommand = { ...step.command, value: step.previous };
			delete plain.at_context_sec;
			delete plain.step_id;
			try {
				if (!(await _dispatch(plain))) return; // not sent (dispose holds the bus): the step stays for dispose's revoke
			} catch (error) {
				await _end('failed', `a step sent ahead could not be revoked after the clock timeline moved: ${error instanceof Error ? error.message : String(error)}`);
				return;
			}
			_setScheduled(scheduled.filter((kept) => kept !== step));
			fired[step.index] = false;
		}
	}

	/**
	 * A fader, trim or crossfader ramp still moving at plan bar `atBar`: one rule, its end against the bar (Sol P1 #7594:
	 * a ramp that finishes before the bar no longer holds the step back to the page tick). A ramp ending on the bar itself
	 * still holds it, since its last write comes on the tick after its end.
	 */
	function _roomRampRunsInto(atBar: number): boolean {
		const room = (command: Record<string, unknown>) => ROOM_RAMP_TYPES.has(command.type as string);
		if ([...ramps.values()].some((ramp) => room(ramp.command) && _rampRunning(ramp) && ramp.fromBar + ramp.overBars >= atBar)) return true;
		return order!.events.some(
			(other, index) => !fired[index] && 'ramp' in other && room(other.ramp.command) && other.at_bar <= atBar && other.at_bar + other.ramp.over_bars >= atBar
		);
	}

	function _stepContextSec(event: PlanEvent, downbeats: readonly number[]): number | null {
		if (!('command' in event) || !SCHEDULABLE_STEP_TYPES.has(event.command.type as string)) return null;
		// No anchor, or one the deck's timeline has moved past (seek, tempo, loop, load...): fire on the bar instead.
		const anchor = deps.positionAnchor(order!.clock.deck);
		if (anchor === null) return null;
		// The pitch is the anchor's own (same timeline revision), never a separately read, possibly newer one.
		if (!Number.isFinite(anchor.pitch) || anchor.pitch <= 0) throw new Error(`clock deck anchor pitch ${anchor.pitch} is not a tempo`);
		const at = anchor.context_sec + (msAtBar(downbeats, order!.start_bar + event.at_bar) - anchor.position_ms) / anchor.pitch / 1000;
		const now = deps.contextNowSec();
		// The O(1) time test first: the ramp and tempo checks below only run for a step inside the window.
		if (at - now > STEP_LOOKAHEAD_S) return null;
		// A ramp on the same control keeps moving until its step lands, so that step fires on the tick at its bar,
		// after the ramp is settled to it, rather than being sent ahead while the ramp still runs.
		const key = _rampKey(event.command);
		if (_controlMoving((command) => _rampKey(command) === key, event.at_bar, false)) return null;
		// Same rule while the clock deck's tempo moves (an AGENT-32 leader glide, set or ramp, running or due by this
		// step's bar): its bar times are not the current pitch's, so the step fires on its bar on the tick.
		const clockDeck = order!.clock.deck;
		if (_controlMoving((command) => command.type === 'tempo' && command.deck === clockDeck, event.at_bar, true)) return null;
		// Codex BLOCKING #7594: a fader step's silence guard judges the room at send time, so while any ramp that shapes
		// the room (a fader, trim or the crossfader, on either deck) runs or is due by this step's bar, the room at the
		// step's own time is not the one judged: the step fires on its bar instead, after those ramps have moved.
		// Codex #7594: a step sent ahead runs on the audio clock whether or not the page ticks again, while room ramps
		// advance only on page ticks. So a fader step goes ahead only if the room is safe for it without counting on any
		// ramp (steps on the audio clock due by its bar do count); safe only once a ramp has run, it waits for its bar.
		if (event.command.type === 'fader' && (_roomRampRunsInto(event.at_bar) || _refusal(event.command, undefined, event.at_bar, false) !== null)) return null;
		return Math.max(at, now);
	}

	/**
	 * Whether a ramp is still running at the playhead (plan bar `bar`). A ramp whose end has passed is settled, even
	 * before `_advanceRamps` removes it this tick: every "ramp running" check reads this, never bare map membership.
	 */
	function _rampRunning(ramp: ActiveRamp): boolean {
		return bar === null || bar < ramp.fromBar + ramp.overBars;
	}

	/**
	 * The ONE "is this control moving" predicate the lookahead asks: a ramp on it is running at the playhead, or a ramp
	 * on it (and with `sets`, a set too) has not fired yet but starts at or before `atBar`. Sol P1 on #7594: an unfired
	 * same-control ramp counts as much as a running one.
	 */
	function _controlMoving(match: (command: Record<string, unknown>) => boolean, atBar: number, sets: boolean): boolean {
		if ([...ramps.values()].some((ramp) => match(ramp.command) && _rampRunning(ramp))) return true;
		// Codex P2 #7594: a step already sent ahead on this control and not yet landed: a second one scheduled now would
		// seed its ramp from the value before the first lands. It fires on its bar instead, once the first has landed.
		if (_waiting().some((step) => match(step.command))) return true;
		return order!.events.some((event, index) => {
			if (fired[index] || event.at_bar > atBar) return false;
			if ('ramp' in event) return match(event.ramp.command);
			return sets && match(event.command);
		});
	}

	/** One tick at a time. `inFlight` is the pass under way, which `amend` waits for before it reads the plan. */
	async function tick(): Promise<void> {
		// Sol P1 #7594: a tick that arrives during an amendment is queued, not dropped: it runs once the amendment ends.
		if (amending) {
			tickQueued = true;
			return;
		}
		if (!active() || ticking || closing) return;
		_refreshOverlay();
		ticking = true;
		inFlight = _advance();
		await inFlight;
	}

	/**
	 * Move every active ramp that has started by plan bar `at` to its value there. A delayed tick can pass several
	 * ramp and event bars at once, so this runs before each event fires: that event then reads the mixer where the
	 * ramps it follows actually land. False once the plan has ended.
	 */
	async function _advanceRamps(at: number): Promise<boolean> {
		for (const [key, ramp] of [...ramps]) {
			if (at < ramp.fromBar) continue;
			// Each ramp dispatch is awaited, and the operator may replace a track or move the transport in that time: both are re-read before the next ramp step.
			const state = deps.query();
			const lost = _lostTrack(state) ?? _transportLost(state, at);
			if (lost !== null) {
				await _end('failed', lost);
				return false;
			}
			const progress = Math.min(1, Math.max(0, (at - ramp.fromBar) / ramp.overBars));
			const value = ramp.from + (ramp.to - ramp.from) * progress;
			// A close that waits for the launch is skipped this pass: the ramp is still active, so the next pass moves it.
			if (_closeAwaitsLaunch(ramp.command, value)) continue;
			const refused = _tempoRangeRefusal(ramp.command, ramp.to) ?? _refusal(ramp.command, value, at);
			if (refused !== null) {
				await _end('failed', refused);
				return false;
			}
			await _dispatch(_withValue(ramp.command, value));
			if (progress >= 1) ramps.delete(key);
			if (!active()) return false;
		}
		return true;
	}

	/** Fail the plan when either deck no longer holds its track; true while both still do. */
	async function _tracksHeld(): Promise<boolean> {
		const lost = _lostTrack(deps.query());
		if (lost === null) return true;
		await _end('failed', lost);
		return false;
	}

	async function _advance(): Promise<void> {
		try {
			const plan = order!;
			const state = deps.query();
			const lost = _lostTrack(state);
			if (lost !== null) {
				await _end('failed', lost);
				return;
			}
			const early = _incomingEarlyStart(state);
			if (early !== null) {
				await _end('failed', early);
				return;
			}
			const stopped = _incomingStopped(state);
			if (stopped !== null) {
				await _end('failed', stopped);
				return;
			}
			const clock = state.decks[plan.clock.deck];
			const downbeats = downbeatsMs(clock.beatgrid);
			bar = barAt(downbeats, clock.position_ms) - plan.start_bar;
			const rejudge = _retireAndRejudge(() => _stepsOffTimeline(downbeats));
			if (rejudge.length > 0) {
				await _revokeOffTimeline(rejudge);
				if (!active()) return;
			}
			if (!clock.playing) {
				// A deck that ran out in the plan's last bar has finished its job:
				// complete the handover rather than strand the incoming deck.
				if (bar >= plan.bars - 1) {
					reason = 'the clock deck stopped in the last bar; handover completed';
					await _finishNow();
				} else {
					await _end('failed', `clock deck ${plan.clock.deck} stopped at plan bar ${bar.toFixed(2)}`);
				}
				return;
			}
			// Read the order on every pass, not the tick's snapshot: an extend that
			// lands while a dispatch is awaited moves the unfired events later.
			// Claude P1 on #7594: a step is sent ahead only while every event before it in plan order has fired, so its
			// guards judge the room those events leave (a crossfader or trim opening just before a cut, say).
			let earlierPending = false;
			for (let index = 0; index < order!.events.length; index += 1) {
				if (fired[index]) continue;
				const event = order!.events[index];
				const atBar = event.at_bar;
				const scheduledAt = atBar > bar && !earlierPending ? _stepContextSec(event, downbeats) : null;
				if (atBar > bar && scheduledAt === null) {
					earlierPending = true;
					continue;
				}
				// A step scheduled ahead moves the ramps only to the playhead: the ramps' own bar is still to come.
				if (!(await _advanceRamps(Math.min(atBar, bar)))) return;
				// A dispatch above, or an order that landed meanwhile, may have replaced a track: no further event fires on it.
				if (!(await _tracksHeld())) return;
				if (status === 'scheduled') status = 'running';
				await _fire(index, atBar, scheduledAt);
				if (!active()) return;
			}
			if (!(await _advanceRamps(bar))) return;
			// The last dispatch above is awaited too: a deck elected in that window must still fail the plan before it is marked done.
			if (!(await _tracksHeld())) return;
			// A launch still armed for its downbeat is part of the plan: the plan ends once it has landed.
			const launchPending = plan.incoming !== null && _armedOnly(deps.query().decks[plan.incoming.deck]);
			// Codex P2/BLOCKING #7594: a step sent ahead is part of the plan until it lands, so abort and dispose can
			// still revoke it and no next plan starts under it.
			// Blind review P1 (#7594): a step the engine lost during an await in this tick is pending too, not finished; the
			// next tick unfires it and sends it again. Only landed and cancelled steps are done.
			const stepPending = scheduled.some((step) => !['landed', 'cancelled'].includes(deps.stepState(step.id) as string));
			if (fired.every(Boolean) && ramps.size === 0 && !launchPending && !stepPending) {
				// The final dispatch above is awaited too: a pause that lands during it fails the plan rather than completing it.
				const lostAtEnd = _lostTrack(deps.query()) ?? _incomingStopped(deps.query());
				if (lostAtEnd !== null) {
					await _end('failed', lostAtEnd);
					return;
				}
				_stop('done', null);
			}
		} catch (error) {
			await _end('failed', error instanceof Error ? error.message : String(error));
		} finally {
			ticking = false;
		}
	}

	/** Why the clock deck can no longer carry the plan, or null: it holds another track or is not playing. */
	function _clockRefusal(plan: AgentPlanOrder): string | null {
		const clock = deps.query().decks[plan.clock.deck];
		if (clock.stable_id !== plan.clock.stable_id) return `clock deck ${plan.clock.deck} does not hold ${plan.clock.stable_id}`;
		if (!clock.playing) return `clock_not_playing: deck ${plan.clock.deck} must be playing`;
		return null;
	}

	/**
	 * Why a tempo target falls outside its deck's live pitch range, or null. Read live, not at order time: the operator
	 * may narrow the range after the order is accepted, and the engine refuses a ratio outside it.
	 */
	/** A discrete tempo set is checked against the live pitch range like a ramp target, so a narrowed range refuses it. */
	function _tempoSetRefusal(command: PlanCommand): string | null {
		return command.type === 'tempo' ? _tempoRangeRefusal(command, Number(command.ratio)) : null;
	}

	function _tempoRangeRefusal(command: PlanCommand, to: number): string | null {
		if (command.type !== 'tempo') return null;
		const deck = command.deck as DeckId;
		const range = deps.query().decks[deck].pitch_range;
		if (typeof range !== 'number' || !Number.isFinite(range)) return `deck ${deck} pitch range is unknown`;
		const min = Math.max(0.01, 1 - range / 100);
		const max = 1 + range / 100;
		return to < min || to > max ? `tempo ramp to ${to} is outside deck ${deck}'s pitch range, ${min} to ${max}` : null;
	}

	/**
	 * Why the master moved off the clock deck and off the master it had when the plan was scheduled, or null. The plan's
	 * own prepare may make the clock deck master; anything else the operator elects during prepare is refused.
	 */
	function _masterRefusal(plan: AgentPlanOrder, masterAtStart: DeckId | null | undefined): string | null {
		const master = deps.query().master_deck;
		if (master === plan.clock.deck || master === masterAtStart) return null;
		return `master moved to deck ${master} during prepare, but the plan needs deck ${plan.clock.deck} as master`;
	}

	/** Why the clock deck is not master, so a quantized launch would not align to it, or null. */
	function _launchMasterRefusal(plan: AgentPlanOrder): string | null {
		const master = deps.query().master_deck;
		return master === plan.clock.deck ? null : `clock deck ${plan.clock.deck} is not master, so the incoming launch would not align to it`;
	}

	/** Why the incoming deck cannot take the plan now, or null: it holds another track, plays, or is armed for a launch. */
	function _incomingRefusal(plan: AgentPlanOrder): string | null {
		if (plan.incoming === null) return null;
		const incoming = deps.query().decks[plan.incoming.deck];
		if (incoming.stable_id !== plan.incoming.stable_id) {
			return `incoming deck ${plan.incoming.deck} does not hold ${plan.incoming.stable_id}`;
		}
		if (incoming.playing) return `incoming deck ${plan.incoming.deck} is already playing`;
		// A launch armed before the plan would start the deck on its own downbeat, ahead of the plan's play; refuse it.
		if (_armedOnly(incoming)) return `incoming deck ${plan.incoming.deck} is already armed to launch`;
		return null;
	}

	async function schedule(payload: unknown): Promise<{ plan_id: string; status: AgentPlanStatus; bar: number }> {
		const plan = parsePlanOrder(payload);
		if (active()) throw new Error(`plan_busy: plan ${order!.id} is ${status}; abort or finish it first`);
		// Sol P1 #7594 (the class): an ended plan whose revoke was refused still has that step armed. It is revoked before
		// the new plan replaces the list, and a refusal refuses the new plan rather than orphaning the step.
		await _revokeLeftoverSteps();
		if (active()) throw new Error(`plan_busy: plan ${order!.id} is ${status}; abort or finish it first`);
		// Checked with the other refusals, before any await: a handoff already in flight would share the incoming deck.
		if (deps.handoffInFlight()) {
			throw new Error('autoplay_handoff: AutoPlay is handing off a deck; schedule the plan once it lands');
		}
		const state = deps.query();
		const clockRefusal = _clockRefusal(plan);
		if (clockRefusal !== null) throw new Error(clockRefusal);
		const clock = state.decks[plan.clock.deck];
		const downbeats = downbeatsMs(clock.beatgrid);
		if (downbeats.length < 2) throw new Error(`no_grid: deck ${plan.clock.deck} has no downbeats`);
		if (plan.start_bar + plan.bars > downbeats.length) {
			throw new Error(`plan runs past the clock deck's grid (bar ${plan.start_bar + plan.bars}, grid ends at ${downbeats.length})`);
		}
		const refusal = _incomingRefusal(plan);
		if (refusal !== null) throw new Error(refusal);
		// A plan with no master prepare command relies on the clock deck already being master, so it is refused before any dispatch.
		const makesMaster = plan.prepare.some((command) => command.type === 'master');
		if (!makesMaster) {
			const notMaster = _launchMasterRefusal(plan);
			if (notMaster !== null) throw new Error(notMaster);
		}
		const masterAtStart = state.master_deck;
		const now = barAt(downbeats, clock.position_ms) - plan.start_bar;
		if (now >= plan.events[0].at_bar) {
			throw new Error(`start_passed: the clock deck is at plan bar ${now.toFixed(2)}, past the first event at ${plan.events[0].at_bar}`);
		}
		order = plan;
		status = 'scheduled';
		reason = null;
		fired = plan.events.map(() => false);
		bar = now;
		endedAt = null;
		ramps.clear();
		// A previous plan's steps belong to it: a later plan's abort must never revoke them.
		_setScheduled([]);
		try {
			// Once prepare has made the clock deck master, the outgoing master is no longer an allowed master: only the clock deck is.
			let promoted = false;
			for (const command of plan.prepare) {
				await _dispatch(command);
				if (deps.query().master_deck === plan.clock.deck) promoted = true;
				// The operator may start, arm, stop or replace a deck while this command is awaited: no later command touches
				// a deck that no longer holds the plan's track, and the plan does not arm over it.
				const taken = _incomingRefusal(plan) ?? _clockRefusal(plan) ?? _masterRefusal(plan, promoted ? plan.clock.deck : masterAtStart);
				if (taken !== null) throw new Error(taken);
			}
			// Armed over prepare's own master command, a launch aligns to the clock deck only if it is still master.
			const unmastered = _launchMasterRefusal(plan);
			if (unmastered !== null) throw new Error(unmastered);
		} catch (error) {
			_stop('failed', `prepare failed: ${error instanceof Error ? error.message : String(error)}`);
			throw error;
		}
		// Taken after prepare, so what prepare set is never mistaken for a knob the operator moved.
		overlayChannels = _channelSnapshot(deps.query());
		overlayLiveKnobs = false;
		_refreshOverlay();
		_arm();
		return { plan_id: plan.id, status, bar: now };
	}

	async function amend(payload: unknown): Promise<{ plan_id: string; status: AgentPlanStatus; bars: number }> {
		const body = _record(payload, 'plan_amend');
		const action = body.action as PlanAmendAction;
		// Codex BLOCKING #7594 (the class): every amendment is ONE transaction. It waits for the tick or amendment in
		// flight, then runs with ticks held out (`ticking`) and `inFlight` set to it, so no tick can interleave with any
		// await inside an amendment (a revoke, a dispatch, an order update), and a second amendment queues behind it.
		while (ticking) await inFlight;
		await inFlight;
		while (ticking) await inFlight;
		if (order === null || body.id !== order.id) throw new Error(`no plan ${String(body.id)} on this page`);
		if (!active()) throw new Error(`plan ${order.id} is already ${status}`);
		return _amendTransaction(() => _applyAmend(action, body));
	}

	function _amendTransaction<T>(work: () => Promise<T>): Promise<T> {
		ticking = true;
		amending = true;
		const run = (async () => {
			try {
				return await work();
			} finally {
				ticking = false;
				amending = false;
				if (tickQueued) {
					tickQueued = false;
					void tick(); // the tick the amendment held out, run now against the amended plan
				}
			}
		})();
		inFlight = run.then(
			() => undefined,
			() => undefined
		);
		return run;
	}

	async function _applyAmend(action: PlanAmendAction, body: Record<string, unknown>): Promise<{ plan_id: string; status: AgentPlanStatus; bars: number }> {
		if (order === null) throw new Error('no plan on this page');
		// Blind review P1 (#7594): a step lost since the last tick is unfired before any amendment reads the list, so
		// finish_now applies it and extend moves it with the rest instead of leaving it fired at its old bar.
		const rejudge = _retireAndRejudge(() => []);
		if (rejudge.length > 0) await _revokeOffTimeline(rejudge);
		if (!active()) throw new Error(`plan_failed: ${reason ?? status}`);
		if (action === 'abort') {
			await _end('aborted', 'aborted by request');
		} else if (action === 'finish_now') {
			const lost = _lostTrack(deps.query());
			if (lost !== null) {
				await _end('failed', lost);
				throw new Error(`plan_failed: ${lost}`);
			}
			// A pause the next tick has not reached yet still fails here, before the terminal events stop the outgoing deck.
			const stopped = _incomingStopped(deps.query());
			if (stopped !== null) {
				await _end('failed', stopped);
				throw new Error(`plan_failed: ${stopped}`);
			}
			reason = 'finished early by request';
			// Inside the amendment transaction, so a tick never runs _advance() while finish_now awaits a dispatch (Codex P1, #5822).
			try {
				await _finishNow();
			} catch (error) {
				// A dispatch that fails partway leaves the plan scheduled or running; end it rather than strand it.
				if (active()) await _end('failed', `finish_now failed: ${error instanceof Error ? error.message : String(error)}`);
				throw error;
			}
		} else if (action === 'extend') {
			const extra = _finite(body.bars, 'plan_amend.bars');
			if (!Number.isInteger(extra) || extra <= 0) throw new RangeError('plan_amend.bars must be a whole number > 0');
			const clock = deps.query().decks[order.clock.deck];
			const downbeats = downbeatsMs(clock.beatgrid);
			if (order.start_bar + order.bars + extra > downbeats.length) {
				throw new Error(`extend_past_grid: ${extra} more bars run past the clock deck's grid`);
			}
			const incoming = order.incoming === null ? null : deps.query().decks[order.incoming.deck];
			if (incoming !== null && (incoming.playing || _armedOnly(incoming))) {
				// Playing, or armed to start on its downbeat (the fired play is not
				// shifted), the incoming deck plays every added bar too.
				const inDownbeats = downbeatsMs(incoming.beatgrid);
				// Bounded at the fastest incoming and slowest clock tempo the plan sets, so a ramp still to come counts too.
				const perClockBar = _gridBarsPerClockBar(clock, incoming, {
					clock: _tempoTargets(order.events, order.clock.deck),
					incoming: _tempoTargets(order.events, order.incoming?.deck),
				});
				if (perClockBar === null) {
					throw new Error('extend_past_grid: the decks report no tempo, so the incoming grid cannot be measured');
				}
				const left = order.bars + extra - (barAt(downbeats, clock.position_ms) - order.start_bar);
				if (barAt(inDownbeats, incoming.position_ms) + left * perClockBar > inDownbeats.length) {
					throw new Error(`extend_past_grid: ${extra} more bars run past the incoming deck's grid`);
				}
			}
			// Once the outgoing deck's handover stop has fired, the room is already resting on the incoming deck:
			// a longer plan would keep the clock stopped before its new last bar.
			// Either terminal half of the handover (the incoming master promotion or the outgoing stop) ends the plan's length.
			const handedOver = order.events.some(
				(event, index) =>
					fired[index] &&
					'command' in event &&
					((event.command.deck === order!.clock.deck && event.command.playing === false) ||
						(event.command.type === 'master' && order!.incoming !== null && event.command.deck === order!.incoming.deck))
			);
			if (handedOver) throw new Error('extend_after_handover: the handover already fired, so the plan can no longer be lengthened');
			// Sol P1 #7594: a step sent ahead but not landed belongs to its old bar. Revoke it and mark it unfired, so it
			// moves with the other unfired events and is scheduled again at its new bar.
			const moving = _waiting().map((step) => step.index);
			try {
				await _settleScheduled('revoke');
			} finally {
				// Codex #7594: a step whose revoke went through is unfired even when another's was refused (that one stays
				// tracked as waiting), so the bookkeeping matches the engine whichever way the extend ends.
				const stillWaiting = new Set(scheduled.map((step) => step.index));
				for (const index of moving) if (!stillWaiting.has(index)) fired[index] = false;
			}
			order = {
				...order,
				bars: order.bars + extra,
				events: order.events.map((event, index) => (fired[index] ? event : { ...event, at_bar: event.at_bar + extra }))
			};
		} else {
			throw new TypeError('plan_amend.action must be abort, finish_now or extend');
		}
		return { plan_id: order.id, status, bars: order.bars };
	}

	function mirror(): AgentPlanMirror | null {
		if (order === null) return null;
		if (endedAt !== null && deps.now() - endedAt > PLAN_MIRROR_KEEP_MS) return null;
		return {
			id: order.id,
			style: order.style,
			status,
			clock_deck: order.clock.deck,
			incoming_deck: order.incoming?.deck ?? null,
			start_bar: order.start_bar,
			bars: order.bars,
			bar: bar === null ? null : Math.round(bar * 100) / 100,
			fired: fired.filter(Boolean).length,
			total: fired.length,
			reason,
			overlay
		};
	}

	return {
		schedule,
		amend,
		mirror,
		tick,
		dispose: () => (disposing ??= _closeForDispose())
	};
}
