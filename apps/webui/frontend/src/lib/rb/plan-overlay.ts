/**
 * WAVEPLAN-7896..7896d: a scheduled plan's automation as geometry on each deck's own track time.
 *
 * A plan is keyed to bars on the clock deck, so the clock deck's curves are fixed by its grid. The incoming deck is
 * different: nothing in the plan pins it, it simply plays from wherever it stands when the launch fires. So its plan
 * bars are read from where it stands NOW, and any move (cue, scrub, nudge, beat jump, loop) moves the curves with it.
 *
 * This is the shape the executor publishes on `agent_plan.overlay`, so an agent reads what the waveform shows.
 */
import { barAt, msAtBar } from './plan-bars';
import { PLAN_KNOBS, type PlanKnob } from './plan-knobs';
import { segmentsWithin, type Point } from '$lib/tryer/tryer-lanes';

/** Drawn whatever the plan does: a flat line says the plan leaves it alone. */
const ALWAYS_DRAWN: ReadonlySet<PlanKnob> = new Set(['fader', 'eq_low']);

/** Under this the incoming deck has not moved: playhead samples jitter by a device buffer. About a pixel on the row. */
const ALIGNMENT_TOLERANCE_MS = 20;
/** A fifth of a beat: past any sync correction, short of a real cue move. */
const ANCHOR_MOVED_BARS = 0.05;

export interface PlanCurve {
	name: PlanKnob;
	/** `[track_ms, knob 0..1]`, in time order; a step repeats its time. */
	points: [number, number][];
}

export interface PlanDeckOverlay {
	deck: number;
	role: 'clock' | 'incoming';
	stable_id: string;
	/** The span of this deck's track the plan covers, or null when `unavailable`. */
	start_ms: number | null;
	end_ms: number | null;
	curves: PlanCurve[];
	/** Why nothing can be placed on this deck: `no_grid`, `no_tempo`, `track_changed` or `build_failed`; else null. */
	unavailable: string | null;
}

export interface PlanOverlay {
	bars: number;
	decks: PlanDeckOverlay[];
	/** The incoming deck no longer stands where the plan was made for it: its owner should plan again. */
	anchor_moved: { planned_bar: number; live_bar: number; moved_bars: number } | null;
}

type OverlayCommand = Record<string, unknown>;
type OverlayEvent =
	| { at_bar: number; command: OverlayCommand }
	| { at_bar: number; ramp: { command: OverlayCommand; to: number; over_bars: number } };

export interface PlanOverlayInput {
	order: {
		clock: { deck: number; stable_id: string };
		incoming: { deck: number; stable_id: string } | null;
		start_bar: number;
		bars: number;
		prepare: readonly OverlayCommand[];
		events: readonly OverlayEvent[];
	};
	/** Each deck's knobs once the plan was prepared, refreshed when one moves: where its first move starts from. */
	channels: Record<number, object>;
	/** A knob moved after preparation, so `channels` outranks the values the plan prepared. */
	liveKnobs: boolean;
	clock: { stable_id: string | null; position_ms: number; downbeats: readonly number[] };
	incoming: {
		stable_id: string | null;
		position_ms: number;
		downbeats: readonly number[];
		playing: boolean;
	} | null;
	/** Incoming grid bars per clock bar at the live tempos, or null when a deck reports none. */
	gridBarsPerClockBar: number | null;
}

/** Where the incoming deck stands against the plan: its grid bar at plan bar 0, and how fast it moves. */
export interface IncomingAlignment {
	stable_id: string;
	origin_bar: number;
	ratio: number;
	/** The span drawn on the incoming track: a tempo change moves its far end by the most. */
	start_ms: number;
	end_ms: number;
}

interface KnobMove {
	at: number;
	to: number;
	over: number;
}

// ------------------------------------------------------------------ knobs ---

function _knobOf(command: OverlayCommand): PlanKnob | null {
	if (command.type === 'fader' || command.type === 'filter') return command.type;
	if (command.type !== 'eq') return null;
	const knob = `eq_${String(command.band)}`;
	return knob === 'eq_low' || knob === 'eq_mid' || knob === 'eq_high' ? knob : null;
}

function _moves(events: readonly OverlayEvent[], deck: number, knob: PlanKnob): KnobMove[] {
	return events.flatMap((event) => {
		const command = 'ramp' in event ? event.ramp.command : event.command;
		if (command.deck !== deck || _knobOf(command) !== knob) return [];
		if ('ramp' in event)
			return [{ at: event.at_bar, to: event.ramp.to, over: event.ramp.over_bars }];
		return [{ at: event.at_bar, to: command.value as number, over: 0 }];
	});
}

/** A knob's plan-bar breakpoints as the executor moves it: a ramp runs from the value it finds, and a later move on
 *  the same knob replaces a ramp still under way (`_fire`, apps/autodj/plan.py `_lane_value`). */
function _breakpoints(
	moves: readonly KnobMove[],
	start: number,
	fromBar: number,
	toBar: number
): Point[] {
	const points: Point[] = [[Math.min(fromBar, moves[0]?.at ?? fromBar), start]];
	let value = start;
	let ramp: (KnobMove & { from: number }) | null = null;
	const settle = (at: number) => {
		if (ramp !== null) {
			const done = Math.min(1, (at - ramp.at) / ramp.over);
			value = ramp.from + (ramp.to - ramp.from) * done;
			points.push([Math.min(at, ramp.at + ramp.over), value]);
		}
		ramp = null;
	};
	for (const move of moves) {
		settle(move.at);
		if (points[points.length - 1][0] !== move.at) points.push([move.at, value]);
		if (move.over === 0) {
			value = move.to;
			points.push([move.at, value]);
		} else ramp = { ...move, from: value };
	}
	const last = ramp as (KnobMove & { from: number }) | null;
	if (last !== null) settle(last.at + last.over);
	points.push([Math.max(toBar, points[points.length - 1][0]), value]);
	return points;
}

function _startValue(
	{ order, channels, liveKnobs }: PlanOverlayInput,
	deck: number,
	knob: PlanKnob
): number {
	const prepared = liveKnobs
		? undefined
		: order.prepare.findLast((command) => command.deck === deck && _knobOf(command) === knob);
	const value =
		prepared === undefined ? (channels[deck] as Record<string, unknown>)?.[knob] : prepared.value;
	if (typeof value !== 'number' || !Number.isFinite(value))
		throw new Error(`no ${knob} value for deck ${deck}`);
	return value;
}

function _deckCurves(
	input: PlanOverlayInput,
	deck: number,
	spans: readonly [number, number][],
	barToMs: (bar: number) => number,
	destBar: (planBar: number) => number
): PlanCurve[] {
	const fromBar = spans[0]?.[0] ?? 0;
	const toBar = spans[spans.length - 1]?.[1] ?? input.order.bars;
	return PLAN_KNOBS.flatMap(({ name }) => {
		const moves = _moves(input.order.events, deck, name);
		if (moves.length === 0 && !ALWAYS_DRAWN.has(name)) return [];
		let bars = _breakpoints(moves, _startValue(input, deck, name), fromBar, toBar);
		bars = _densifyForDestGrid(bars, destBar);
		const pieces = segmentsWithin(bars, spans);
		const points = pieces.flatMap((segment) =>
			segment.map(([bar, value]) => [barToMs(bar), value] as [number, number])
		);
		return [{ name, points }];
	});
}

/** Insert samples at each destination-grid integer a ramp crosses, so canvas lines match bar-linear execution. */
function _densifyForDestGrid(points: Point[], destBar: (planBar: number) => number): Point[] {
	if (points.length < 2) return points;
	const extra: Point[] = [];
	for (let i = 0; i < points.length - 1; i++) {
		const [planFrom, valueFrom] = points[i];
		const [planTo, valueTo] = points[i + 1];
		if (valueFrom === valueTo || planFrom === planTo) continue;
		const destFrom = destBar(planFrom);
		const destTo = destBar(planTo);
		if (destFrom === destTo) continue;
		const lo = Math.min(destFrom, destTo);
		const hi = Math.max(destFrom, destTo);
		const firstK = Math.floor(lo) + 1;
		const lastK = Math.ceil(hi) - 1;
		for (let k = firstK; k <= lastK; k++) {
			if (!(k > lo && k < hi)) continue;
			let left = Math.min(planFrom, planTo);
			let right = Math.max(planFrom, planTo);
			for (let iter = 0; iter < 48; iter++) {
				const mid = (left + right) / 2;
				if (destBar(mid) < k) left = mid;
				else right = mid;
			}
			const planBar = (left + right) / 2;
			const t = (planBar - planFrom) / (planTo - planFrom);
			extra.push([planBar, valueFrom + (valueTo - valueFrom) * t]);
		}
	}
	if (extra.length === 0) return points;
	return [...points, ...extra].sort((a, b) => a[0] - b[0]);
}

// ------------------------------------------------------------- transport ---

type PlayEvent = Extract<OverlayEvent, { command: OverlayCommand }>;

function _effectivePlayBar(event: PlayEvent): number {
	const { quantize } = event.command;
	return quantize === true ? Math.floor(event.at_bar) + 1 : event.at_bar;
}

/** Each play or stop as `(bar it takes effect, playing after it)`, in effect order (mirrors autodj `_transport_steps`). */
function _transportSteps(order: PlanOverlayInput['order'], deck: number): [number, boolean][] {
	const events = order.events
		.filter(
			(event): event is PlayEvent =>
				'command' in event && event.command.type === 'play' && event.command.deck === deck
		)
		.sort((a, b) => a.at_bar - b.at_bar);
	const steps: [number, boolean][] = [];
	let armed: number | null = null;
	for (const event of events) {
		if (armed !== null && steps[armed][0] > event.at_bar)
			steps[armed] = [Number.POSITIVE_INFINITY, false];
		const playing = event.command.playing === true;
		if (playing) {
			steps.push([_effectivePlayBar(event), true]);
			armed = steps.length - 1;
		} else {
			steps.push([event.at_bar, false]);
			armed = null;
		}
	}
	return steps.filter((step) => step[0] !== Number.POSITIVE_INFINITY).sort((a, b) => a[0] - b[0]);
}

function _playingIntervals(order: PlanOverlayInput['order'], deck: number): [number, number][] {
	const intervals: [number, number][] = [];
	let since: number | null = null;
	for (const [effective, isPlay] of _transportSteps(order, deck)) {
		if (isPlay && since === null) since = effective;
		else if (!isPlay && since !== null) {
			intervals.push([since, effective]);
			since = null;
		}
	}
	if (since !== null) intervals.push([since, order.bars]);
	return intervals
		.map(([start, end]) => [start, Math.min(end, order.bars)] as [number, number])
		.filter(([start, end]) => end > start);
}

function _playingBarsBefore(spans: readonly [number, number][], planBar: number): number {
	let total = 0;
	for (const [start, end] of spans) total += Math.max(0, Math.min(end, planBar) - start);
	return total;
}

/** The plan bar a deck's stop fires at, else the plan's end. */
function _stopBar(order: PlanOverlayInput['order'], deck: number): number {
	const stop = order.events.find(
		(event) =>
			'command' in event &&
			event.command.type === 'play' &&
			event.command.deck === deck &&
			event.command.playing === false
	);
	return stop === undefined ? order.bars : Math.min(stop.at_bar, order.bars);
}

/** The plan bar the incoming deck first plays from, after transport cancellation. */
function _launchBar(order: PlanOverlayInput['order'], deck: number): number {
	const intervals = _playingIntervals(order, deck);
	return intervals.length === 0 ? 0 : intervals[0][0];
}

function _incomingSpans(order: PlanOverlayInput['order'], deck: number): [number, number][] {
	const intervals = _playingIntervals(order, deck);
	return intervals.length === 0 ? [[0, order.bars]] : intervals;
}

/** The cue the plan was made for: the incoming grid bar it launches from, or null when the plan does not cue the deck. */
function _plannedBar(input: PlanOverlayInput): number | null {
	const { order, incoming } = input;
	if (order.incoming === null || incoming === null) return null;
	const deck = order.incoming.deck;
	const cue = order.prepare.findLast((command) => command.type === 'seek' && command.deck === deck);
	return cue === undefined ? null : barAt(incoming.downbeats, cue.position_ms as number);
}

function _hasTempoMoves(order: PlanOverlayInput['order']): boolean {
	return order.events.some(
		(event) => ('ramp' in event ? event.ramp.command : event.command).type === 'tempo'
	);
}

/** Read the incoming deck against the plan, from a single pair of playhead samples; null when it cannot be placed. */
export function incomingAlignment(input: PlanOverlayInput): IncomingAlignment | null {
	const { order, clock, incoming, gridBarsPerClockBar: ratio } = input;
	if (
		order.incoming === null ||
		incoming === null ||
		incoming.stable_id !== order.incoming.stable_id
	)
		return null;
	if (incoming.downbeats.length < 2 || clock.downbeats.length < 2 || ratio === null) return null;
	const deck = order.incoming.deck;
	const spans = _incomingSpans(order, deck);
	const firstLaunch = _launchBar(order, deck);
	const anchorPlanBar = incoming.playing
		? barAt(clock.downbeats, clock.position_ms) - order.start_bar
		: firstLaunch;
	const origin_bar =
		barAt(incoming.downbeats, incoming.position_ms) -
		_playingBarsBefore(spans, anchorPlanBar) * ratio;
	const barToMs = (bar: number) =>
		msAtBar(incoming.downbeats, origin_bar + _playingBarsBefore(spans, bar) * ratio);
	const lastEnd = spans[spans.length - 1][1];
	return {
		stable_id: order.incoming.stable_id,
		origin_bar,
		ratio,
		start_ms: barToMs(firstLaunch),
		end_ms: barToMs(lastEnd)
	};
}

/** Whether the geometry must be rebuilt: called per executor tick, so it answers without allocating. */
export function alignmentChanged(
	drawn: IncomingAlignment | null,
	live: IncomingAlignment | null
): boolean {
	if (drawn === null || live === null) return drawn !== live;
	return (
		drawn.stable_id !== live.stable_id ||
		Math.abs(drawn.start_ms - live.start_ms) > ALIGNMENT_TOLERANCE_MS ||
		Math.abs(drawn.end_ms - live.end_ms) > ALIGNMENT_TOLERANCE_MS
	);
}

// ------------------------------------------------------------------ build ---

function _rounded(curve: PlanCurve): PlanCurve {
	const points = curve.points.map(
		([ms, value]) =>
			[Math.round(ms * 10) / 10, Math.round(value * 10_000) / 10_000] as [number, number]
	);
	return { name: curve.name, points };
}

function _clockDeck(input: PlanOverlayInput): PlanDeckOverlay {
	const { order, clock } = input;
	const { deck, stable_id } = order.clock;
	const blank = {
		deck,
		role: 'clock' as const,
		stable_id,
		start_ms: null,
		end_ms: null,
		curves: []
	};
	if (clock.stable_id !== stable_id) return { ...blank, unavailable: 'track_changed' };
	if (clock.downbeats.length < 2) return { ...blank, unavailable: 'no_grid' };
	const barToMs = (bar: number) => msAtBar(clock.downbeats, order.start_bar + bar);
	const toBar = _stopBar(order, deck);
	const destBar = (planBar: number) => order.start_bar + planBar;
	return {
		...blank,
		start_ms: barToMs(0),
		end_ms: barToMs(toBar),
		curves: _deckCurves(input, deck, [[0, toBar]], barToMs, destBar).map(_rounded),
		unavailable: null
	};
}

function _incomingUnavailable(input: PlanOverlayInput): string {
	const { order, incoming, gridBarsPerClockBar } = input;
	if (incoming === null || incoming.stable_id !== order.incoming?.stable_id) return 'track_changed';
	return gridBarsPerClockBar === null ? 'no_tempo' : 'no_grid';
}

function _incomingDeck(
	input: PlanOverlayInput,
	alignment: IncomingAlignment | null
): PlanDeckOverlay | null {
	const { order, incoming } = input;
	if (order.incoming === null) return null;
	const { deck, stable_id } = order.incoming;
	const blank = {
		deck,
		role: 'incoming' as const,
		stable_id,
		start_ms: null,
		end_ms: null,
		curves: []
	};
	if (alignment === null || incoming === null)
		return { ...blank, unavailable: _incomingUnavailable(input) };
	const spans = _incomingSpans(order, deck);
	const fromBar = spans[0][0];
	const toBar = spans[spans.length - 1][1];
	const barToMs = (bar: number) =>
		msAtBar(
			incoming.downbeats,
			alignment.origin_bar + _playingBarsBefore(spans, bar) * alignment.ratio
		);
	const destBar = (planBar: number) =>
		alignment.origin_bar + _playingBarsBefore(spans, planBar) * alignment.ratio;
	return {
		...blank,
		start_ms: barToMs(fromBar),
		end_ms: barToMs(toBar),
		curves: _deckCurves(input, deck, spans, barToMs, destBar).map(_rounded),
		unavailable: null
	};
}

function _anchorMoved(
	input: PlanOverlayInput,
	alignment: IncomingAlignment | null
): PlanOverlay['anchor_moved'] {
	const { order, incoming } = input;
	if (alignment === null || order.incoming === null) return null;
	// Past the launch a plan's own tempo moves change the ratio, so a drift there is the plan, not a move.
	if (incoming?.playing === true && _hasTempoMoves(order)) return null;
	const planned = _plannedBar(input);
	if (planned === null) return null;
	const spans = _incomingSpans(order, order.incoming.deck);
	const launch = _launchBar(order, order.incoming.deck);
	const live = alignment.origin_bar + _playingBarsBefore(spans, launch) * alignment.ratio;
	const moved = live - planned;
	if (Math.abs(moved) < ANCHOR_MOVED_BARS) return null;
	const rounded = (bars: number) => Math.round(bars * 100) / 100;
	return { planned_bar: rounded(planned), live_bar: rounded(live), moved_bars: rounded(moved) };
}

/** What a pending plan publishes when its geometry cannot be built: never null, which means no plan is pending. */
export function failedPlanOverlay(order: PlanOverlayInput['order']): PlanOverlay {
	const blank = { start_ms: null, end_ms: null, curves: [], unavailable: 'build_failed' };
	const clock: PlanDeckOverlay = {
		...blank,
		deck: order.clock.deck,
		role: 'clock',
		stable_id: order.clock.stable_id
	};
	const incoming: PlanDeckOverlay | null =
		order.incoming === null
			? null
			: {
					...blank,
					deck: order.incoming.deck,
					role: 'incoming',
					stable_id: order.incoming.stable_id
				};
	return {
		bars: order.bars,
		decks: incoming === null ? [clock] : [clock, incoming],
		anchor_moved: null
	};
}

/** The whole overlay, read from the live decks; `alignment` is passed when the caller has just read it. */
export function buildPlanOverlay(
	input: PlanOverlayInput,
	alignment = incomingAlignment(input)
): PlanOverlay {
	const incoming = _incomingDeck(input, alignment);
	return {
		bars: input.order.bars,
		decks: incoming === null ? [_clockDeck(input)] : [_clockDeck(input), incoming],
		anchor_moved: _anchorMoved(input, alignment)
	};
}
