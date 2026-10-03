/**
 * Land a finished stem bundle on a deck, including a deck that is PLAYING
 * (STEM-47).
 *
 * Supersedes: holding a finished bundle until the deck next stops. That left
 * the stem buttons dead for the whole play of any track started before its
 * stems landed, which is the "song has stems, buttons do nothing" report.
 *
 * The live handoff never replaces a processor mid-buffer. It schedules the stem
 * processor to START at one future context instant, at the position the deck
 * will have reached by then, and schedules the mix processor to STOP at that
 * same instant. Both are the same time-stretch worklet with the same latency
 * and onset ramp, so the deck's clock, position and tempo carry straight on;
 * nothing about the transport timeline changes, only which processor feeds it.
 *
 * Pure orchestration over injected deps, so the ordering and every refusal can
 * be tested without Web Audio (tests/unit/stem-live-handoff.test.mjs). The
 * engine supplies the deps; this module never imports it.
 *
 *   ✔︎ ✅ 🎯 Stems start and the mix stops at ONE instant, stems scheduled first.
 *       [if] the two instants differ [then ⛔️]
 *       [if] the mix is stopped before the stems acknowledged [then ⛔️]
 *       [if] the deck is committed before the stems acknowledged [then ⛔️]
 *   ✔︎ ✅ 🎯 A transport command during the handoff wins; the handoff is canceled.
 *       [if] the schedule revision moved during the ack and it commits [then ⛔️]
 *       [if] a command is still queued and it commits [then ⛔️]
 *       [if] a canceled handoff stops the mix [then ⛔️]
 *       [if] a command sent between the commit and the handoff instant reaches the stems before the mix has stopped [then ⛔️]
 *   ✔︎ ✅ 🎯 A failed or late handoff never disturbs the playing mix.
 *       [if] a rejected stem schedule stops or retires the mix [then ⛔️]
 *       [if] a late acknowledgement commits [then ⛔️]
 *       [if] a mix that refuses its stop keeps playing under the stems [then ⛔️]
 *       [if] a mix that refuses its stop is retired before the stems start [then ⛔️]
 *       [if] a mix whose stop is still unacknowledged when the stems start keeps playing under them [then ⛔️]
 */
import { _positionForSegment, safeTransportScheduleTime } from '$lib/player/transport/schedule-math';
import type { _ClockSegment } from '$lib/player/transport/schedule-math';
import type { LoopState } from '$lib/rb/deck-state-types';
import type { StretchScheduleChange } from '$lib/rb/stretch-adapter';

/** The transport segment the deck will be in at the handoff instant. */
export interface StemHandoffSegment {
	positionSec: number;
	tempoRatio: number;
	masterTempoEnabled: boolean;
	keyShiftSemitones: number;
	loop: LoopState | null;
}

/** Everything that must hold still for a live handoff to be safe. */
export interface StemHandoffSnapshot {
	/** The control clock says the deck is running (not paused, not armed). */
	active: boolean;
	/** Bumped by every transport schedule; a change means a command landed. */
	revision: number;
	/** Transport commands queued or in flight on this deck. */
	intents: number;
	/** Schedules accepted but not yet committed to the control clock. */
	pendingCount: number;
	transportPending: boolean;
	/** A quantized launch is armed: the deck reads playing but is not running. */
	launchArmed: boolean;
	/** Stem and mix processors report the same latency, so one instant means
	 * one instant at the output. */
	latencyMatches: boolean;
}

export interface StemHandoffDeps {
	/** AudioContext.currentTime. */
	now(): number;
	/** The processor's onset lead: how far ahead a start must be scheduled. */
	leadSec: number;
	snapshot(): StemHandoffSnapshot;
	/** Where the deck will be at `when`. Called only on an idle, active deck. */
	segmentAt(when: number): StemHandoffSegment;
	/** Start the stems at `when`, at the segment's position. Resolves on ack.
	 * The stems are not connected to the deck's output yet. */
	scheduleIncoming(when: number, segment: StemHandoffSegment): Promise<void>;
	/** Connect the scheduled stems to the deck's output. Called only once the
	 * acknowledgement is back and `when` is still ahead, so a late schedule is
	 * never audible beside the mix. Synchronous. */
	connectIncoming(): void;
	/** Silence a stem schedule that will not be used: an inactive change at the
	 * SAME instant, which the worklet's time map lets replace the start. */
	cancelIncoming(when: number): Promise<void>;
	/** Stop the mix at `when`. */
	stopOutgoing(when: number): Promise<void>;
	/** The mix refused its stop: keep it audible until `when`, the instant the
	 * stems take over, then retire it, and report `error`. Retiring it at once
	 * would leave the deck silent until `when`. */
	retireRefusedOutgoing(when: number, error: unknown): void;
	/** Make the stems the deck's processor and publish `ready`. Synchronous. */
	commit(when: number, segment: StemHandoffSegment): void;
	/** Queue the deck's transport commands behind `when`. Until then the mix is
	 * still the audible processor and only its scheduled stop reaches it, so a
	 * command sent to the stems alone would double or outlive the mix. */
	holdCommandsUntil(when: number): void;
	/** The load this upgrade belongs to is gone (track swapped, graph rebuilt). */
	stale(): boolean;
	/** The deck is at rest, so the plain stopped swap applies. */
	replaceable(): boolean;
	/** The plain stopped swap. */
	adoptStopped(): void;
	sleep(ms: number): Promise<void>;
}

export type LiveHandoffOutcome = 'handed_off' | 'busy' | 'moved';
export type StemLandingOutcome = 'adopted' | 'handed_off' | 'stale' | 'deferred';

/** Extra room, beyond the processor lead, between scheduling the stems and the
 * handoff instant. It has to cover the worklet acknowledgement for every stem
 * part; each retry gives it more, so one slow acknowledgement is not final. */
export const STEM_HANDOFF_MARGINS_SEC: readonly number[] = [0.15, 0.3, 0.6, 1.2];
/** Wait between attempts while the deck is busy with a transport command. */
export const STEM_HANDOFF_RETRY_MS = 250;

function _idle(snap: StemHandoffSnapshot): boolean {
	return (
		snap.active &&
		snap.intents === 0 &&
		snap.pendingCount === 0 &&
		!snap.transportPending &&
		!snap.launchArmed &&
		snap.latencyMatches
	);
}

/**
 * One live handoff attempt on a playing deck.
 *
 * `busy`: the deck was not idle, nothing was scheduled. `moved`: the deck
 * changed, or the acknowledgement came too late, after the stems were
 * scheduled; that schedule is canceled and the mix is untouched. Either way
 * the caller may try again. A rejected stem schedule propagates.
 */
export async function handOffStemsLive(
	deps: StemHandoffDeps,
	marginSec: number
): Promise<LiveHandoffOutcome> {
	const before = deps.snapshot();
	if (!_idle(before)) return 'busy';
	const when = safeTransportScheduleTime(deps.now(), deps.leadSec) + marginSec;
	const segment = deps.segmentAt(when);
	await deps.scheduleIncoming(when, segment);
	const after = deps.snapshot();
	const stillSchedulable = safeTransportScheduleTime(deps.now(), deps.leadSec) <= when;
	// `stale` is re-read here, not only between attempts: the track can be
	// unloaded, or stems switched off, while the acknowledgement is in flight.
	if (deps.stale() || after.revision !== before.revision || !_idle(after) || !stillSchedulable) {
		await deps.cancelIncoming(when);
		return 'moved';
	}
	// From here to the end is synchronous: no command can interleave between
	// the mix's stop being posted and the deck pointing at the stems.
	deps.connectIncoming();
	void deps.stopOutgoing(when).catch((error: unknown) => deps.retireRefusedOutgoing(when, error));
	deps.commit(when, segment);
	deps.holdCommandsUntil(when);
	return 'handed_off';
}

/**
 * Land a finished stem bundle: the stopped swap when the deck is at rest, the
 * live handoff when it is playing, retried while the deck is busy.
 *
 * `deferred` means every attempt found the deck busy; the caller keeps the
 * bundle and lands it on the next stop, and says so on the deck.
 */
export async function landStemUpgrade(deps: StemHandoffDeps): Promise<StemLandingOutcome> {
	for (let attempt = 0; attempt < STEM_HANDOFF_MARGINS_SEC.length; attempt += 1) {
		if (deps.stale()) return 'stale';
		if (deps.replaceable()) {
			deps.adoptStopped();
			return 'adopted';
		}
		const outcome = await handOffStemsLive(deps, STEM_HANDOFF_MARGINS_SEC[attempt]);
		if (outcome === 'handed_off') return 'handed_off';
		if (attempt < STEM_HANDOFF_MARGINS_SEC.length - 1) await deps.sleep(STEM_HANDOFF_RETRY_MS);
	}
	return deps.stale() ? 'stale' : 'deferred';
}

// ----------------------------------------------------------- engine binding

/** The slice of a deck processor the landing drives. */
export interface StemLandingProcessor {
	connect(destination: AudioNode): void;
	schedule(outputTime: number, change: StretchScheduleChange): Promise<void>;
	stop(outputTime: number): Promise<void>;
}

/** The deck runtime fields the landing reads. The engine passes its own
 * runtime object; nothing here is copied, so every read is live. */
export interface StemLandingRuntime {
	readonly processor: StemLandingProcessor | null;
	readonly nodes: { analyser: AudioNode } | null;
	readonly latencySec: number;
	readonly durationSec: number;
	readonly controlActive: boolean;
	readonly nextScheduleRevision: number;
	readonly scheduleIntentCount: number;
	readonly pending: readonly unknown[];
	/** The deck's transport command queue: every play, pause, seek or tempo
	 * command waits on it before reaching the processor. */
	scheduleTail: Promise<void>;
}

/**
 * What the deck engine hands the landing. Kept here, not in the engine, so the
 * wiring between a deck's runtime and the handoff is one tested function and
 * the engine file carries only the reads and writes that are its own.
 */
export interface StemLandingPort {
	runtime: StemLandingRuntime;
	incoming: StemLandingProcessor & { latencySec(): Promise<number> };
	clock: { readonly currentTime: number; readonly sampleRate: number };
	leadSec: number;
	/** Run the landing serialized with the deck's other processor swaps. */
	serialized<T>(run: () => Promise<T>): Promise<T>;
	/** Commit any schedule that is due, so `runtime` reads are current. */
	commitDue(): void;
	/** A sync ramp is mid-flight (the engine's flag, not the presentation one:
	 * a backgrounded window never runs the loop that clears that). */
	rampPending(): boolean;
	launchArmed(): boolean;
	controlSegmentAt(when: number): _ClockSegment;
	/** The engine's own stretch change for a start at this segment. */
	startChange(segment: StemHandoffSegment): StretchScheduleChange;
	retire(processor: StemLandingProcessor): void;
	/** Point the deck at the stems and publish `ready`. */
	commit(when: number): void;
	/** Keep the bundle for the deck's next stop, and say so on the deck. */
	defer(): void;
	stale(): boolean;
	replaceable(): boolean;
	adoptStopped(): void;
	setTimer?: (run: () => void, ms: number) => unknown;
}

/** How long after the handoff instant the stopped mix is retired: long enough
 * for its stop to have rendered through the stretcher's own tail. */
export const STEM_HANDOFF_RETIRE_AFTER_SEC = 1;

export function stemLandingDeps(port: StemLandingPort, incomingLatencySec: number): StemHandoffDeps {
	const setTimer = port.setTimer ?? ((run: () => void, ms: number) => setTimeout(run, ms));
	const rt = port.runtime;
	let outgoing: StemLandingProcessor | null = null;
	let outgoingRetired = false;
	const retireOutgoingOnce = (): void => {
		if (outgoing === null || outgoingRetired) return;
		outgoingRetired = true;
		port.retire(outgoing);
	};
	return {
		now: () => port.clock.currentTime,
		leadSec: port.leadSec,
		snapshot: () => {
			port.commitDue();
			return {
				active: rt.controlActive,
				revision: rt.nextScheduleRevision,
				intents: rt.scheduleIntentCount,
				pendingCount: rt.pending.length,
				transportPending: port.rampPending(),
				launchArmed: port.launchArmed(),
				latencyMatches: Math.abs(incomingLatencySec - rt.latencySec) <= 1 / port.clock.sampleRate
			};
		},
		segmentAt: (when) => {
			const segment = port.controlSegmentAt(when);
			return {
				positionSec: _positionForSegment(segment, when, rt.durationSec),
				tempoRatio: segment.tempoRatio,
				masterTempoEnabled: segment.masterTempoEnabled ?? true,
				keyShiftSemitones: segment.keyShiftSemitones ?? 0,
				loop: segment.loop
			};
		},
		scheduleIncoming: async (when, segment) => {
			if (rt.nodes === null) throw new Error('stem handoff: the deck audio graph is missing');
			// Not connected yet: an acknowledgement that arrives after `when`
			// would otherwise leave the stems playing beside the mix until the
			// cancel lands. connectIncoming() joins them once the instant is safe.
			await port.incoming.schedule(when, port.startChange(segment));
		},
		connectIncoming: () => {
			if (rt.nodes === null) throw new Error('stem handoff: the deck audio graph is missing');
			port.incoming.connect(rt.nodes.analyser);
		},
		cancelIncoming: (when) => port.incoming.stop(when),
		stopOutgoing: async (when) => {
			outgoing = rt.processor;
			if (outgoing === null) return;
			const retiring = outgoing;
			// Retired once its stop has rendered, never while it is the audible tail.
			const untilWhenSec = Math.max(0, when - port.clock.currentTime);
			// A stop still unacknowledged when the stems start (a slow or timed-out
			// command) must not leave the mix under them: retire it then.
			let stopAcknowledged = false;
			setTimer(() => {
				if (!stopAcknowledged) retireOutgoingOnce();
			}, untilWhenSec * 1000);
			setTimer(retireOutgoingOnce, (untilWhenSec + STEM_HANDOFF_RETIRE_AFTER_SEC) * 1000);
			await retiring.stop(when);
			stopAcknowledged = true;
		},
		retireRefusedOutgoing: (when, error) => {
			console.error('stem handoff: the mix refused its stop; retiring it at the handoff instant', error);
			setTimer(retireOutgoingOnce, Math.max(0, when - port.clock.currentTime) * 1000);
		},
		commit: (when) => port.commit(when),
		holdCommandsUntil: (when) => {
			const queued = rt.scheduleTail;
			const untilMs = Math.max(0, when - port.clock.currentTime) * 1000;
			rt.scheduleTail = queued.then(() => new Promise<void>((resolve) => setTimer(resolve, untilMs)));
		},
		stale: () => port.stale(),
		replaceable: () => port.replaceable(),
		adoptStopped: () => port.adoptStopped(),
		sleep: (ms) => new Promise<void>((resolve) => setTimer(resolve, ms))
	};
}

/**
 * Land a built stem processor on a deck and settle what is left over: a stale
 * bundle is retired, a deferred one is handed back to the deck to hold.
 */
export async function landStemsOnDeck(port: StemLandingPort): Promise<StemLandingOutcome> {
	const incomingLatencySec = await port.incoming.latencySec();
	const outcome = await port.serialized(() =>
		landStemUpgrade(stemLandingDeps(port, incomingLatencySec))
	);
	if (outcome === 'stale') port.retire(port.incoming);
	else if (outcome === 'deferred') port.defer();
	return outcome;
}

/** What `LOAD NOW` on a held bundle needs to settle a failed landing. */
export interface HeldStemLandingPort {
	land(): Promise<unknown>;
	/** The deck already points at the held processor. */
	adopted(): boolean;
	retire(): void;
	stale(): boolean;
	/** Publish a retryable error on the deck. */
	fail(message: string): void;
}

/**
 * Land a bundle held for the deck's next stop, settling the deck if the
 * landing rejects. The landing clears the held reference and shows
 * `switching` before anything is awaited, so without this a rejection left
 * the deck reading `switching` with nothing to retry until the track was
 * reloaded. The rejection still reaches the caller.
 */
export async function landHeldStemsOrSettle(port: HeldStemLandingPort): Promise<void> {
	try {
		await port.land();
	} catch (error) {
		if (!port.adopted()) port.retire();
		if (!port.stale()) port.fail(error instanceof Error ? error.message : String(error));
		throw error;
	}
}
