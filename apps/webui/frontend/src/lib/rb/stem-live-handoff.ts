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
 *   ✔︎ ✅ 🎯 A failed or late handoff never disturbs the playing mix.
 *       [if] a rejected stem schedule stops or retires the mix [then ⛔️]
 *       [if] a late acknowledgement commits [then ⛔️]
 *       [if] the deck commits before the mix acknowledged its stop [then ⛔️]
 *       [if] a refused or timed-out mix stop (which poisons the mix, so it
 *            cannot be put back) leaves the deck on the dead mix, or is
 *            swallowed [then ⛔️]
 *       [if] a mix stop still unacknowledged at the handoff instant leaves the
 *            mix playing under the stems [then ⛔️]
 *       [if] a rollback whose stems cancel fails skips putting the mix back
 *            [then ⛔️]
 *       [if] the stopped mix's retirement is armed before its stop is
 *            acknowledged [then ⛔️]
 */
import { _positionForSegment, safeTransportScheduleTime } from '$lib/player/transport/schedule-math';
import type { _ClockSegment } from '$lib/player/transport/schedule-math';
import type { LoopState } from '$lib/rb/deck-state-types';
import type { StretchScheduleChange } from '$lib/rb/stretch-adapter';

/** The transport segment the deck will be in at the handoff instant. */
export interface StemHandoffSegment {
	/** The control clock says the deck is running at this instant. */
	active: boolean;
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
	/** Start the stems at `when`, at the segment's position. Resolves on ack. */
	scheduleIncoming(when: number, segment: StemHandoffSegment): Promise<void>;
	/** Silence a stem schedule that will not be used: an inactive change at the
	 * SAME instant, which the worklet's time map lets replace the start. */
	cancelIncoming(when: number): Promise<void>;
	/** Stop the mix at `when`. Resolves on ack. */
	stopOutgoing(when: number): Promise<void>;
	/** Undo a posted mix stop: schedule the mix at `at` as the control clock
	 * says the deck is then, which replaces a stop at the same instant. */
	restoreOutgoing(at: number, segment: StemHandoffSegment): Promise<void>;
	/** Retire the stopped mix `tailSec` after `when`. */
	retireOutgoingAfter(when: number, tailSec: number): void;
	/** Cut the mix at `when` if `stillPending()` then says its stop has not
	 * settled. Returns whether that cut happened (read once the stop settles). */
	cutOutgoingAt(when: number, stillPending: () => boolean): () => boolean;
	/** The mix refused its stop or never acknowledged it; the stems took over. */
	reportOutgoingFailure(error: unknown): void;
	/** The mix is dead (poisoned or cut) and the deck still points at it, with
	 * no stems to take over: fail the deck so it can be stopped and unloaded. */
	failOutgoing(error: unknown): void;
	/** Make the stems the deck's processor and publish `ready`. Synchronous. */
	commit(when: number, segment: StemHandoffSegment): void;
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
 * changed, or an acknowledgement came too late, after the stems were
 * scheduled; that schedule is canceled and the mix is put back as it was.
 * Either way the caller may try again. A rejected stem schedule, or a failed
 * rollback, propagates.
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
	// The mix's stop is acknowledged BEFORE the deck points at the stems, and
	// the ack is a second await, so the deck is checked again after it. The
	// stems are already audible from `when`, so a stop still unsettled then is
	// cut at that instant on its own timer: a stalled ack never doubles them.
	const stop: { state: 'pending' | 'acked' | 'failed' } = { state: 'pending' };
	let stopError: unknown = null;
	const stopSettled = deps.stopOutgoing(when).then(
		() => {
			stop.state = 'acked';
		},
		(error: unknown) => {
			stop.state = 'failed';
			stopError = error;
		}
	);
	const wasCut = deps.cutOutgoingAt(when, () => stop.state === 'pending');
	await stopSettled;
	const cut = wasCut();
	if (stop.state === 'failed' || cut) {
		// A refused or timed-out command poisons the mix's processor, and a cut
		// one is disconnected: either way it cannot be put back. The stems'
		// start is acknowledged, so they take the deck and the failure is
		// reported. If the upgrade went stale meanwhile there are no stems to
		// take over, so the deck itself is failed rather than left on a dead mix.
		const reason = stopError ?? new Error('the mix did not acknowledge its stop by the handoff instant');
		if (deps.stale()) {
			deps.failOutgoing(reason);
			return 'moved';
		}
		deps.commit(when, segment);
		if (!cut) deps.retireOutgoingAfter(when, 0);
		deps.reportOutgoingFailure(reason);
		return 'handed_off';
	}
	const settled = deps.snapshot();
	const earliest = safeTransportScheduleTime(deps.now(), deps.leadSec);
	if (deps.stale() || settled.revision !== before.revision || !_idle(settled) || earliest > when) {
		// Undo at `when` while that is still ahead (an exact replacement of
		// both scheduled changes); once it has passed, at the first instant
		// the processors can still take, where the deck then is. The mix is put
		// back even when the stems' cancel fails.
		const at = earliest > when ? earliest : when;
		let rollbackError: unknown = null;
		try {
			await deps.cancelIncoming(at);
		} catch (error) {
			rollbackError = error;
		}
		try {
			await deps.restoreOutgoing(at, deps.segmentAt(at));
		} catch (error) {
			rollbackError ??= error;
		}
		if (rollbackError !== null) throw rollbackError;
		return 'moved';
	}
	// From here to the end is synchronous: no command can interleave between
	// the check above and the deck pointing at the stems.
	deps.commit(when, segment);
	deps.retireOutgoingAfter(when, STEM_HANDOFF_RETIRE_AFTER_SEC);
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
	/** The engine's own stretch change for this segment: a start when it is
	 * active, a hold at its position when it is not. */
	startChange(segment: StemHandoffSegment): StretchScheduleChange;
	retire(processor: StemLandingProcessor): void;
	/** The mix refused or never acknowledged its stop. `terminal`: no stems took
	 * over and the deck still points at the dead mix, so fail the deck. */
	outgoingFailed(error: unknown, terminal: boolean): void;
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
				active: segment.active,
				positionSec: _positionForSegment(segment, when, rt.durationSec),
				tempoRatio: segment.tempoRatio,
				masterTempoEnabled: segment.masterTempoEnabled ?? true,
				keyShiftSemitones: segment.keyShiftSemitones ?? 0,
				loop: segment.loop
			};
		},
		scheduleIncoming: async (when, segment) => {
			if (rt.nodes === null) throw new Error('stem handoff: the deck audio graph is missing');
			// Connected first, and silent until `when`: its time map is inactive.
			port.incoming.connect(rt.nodes.analyser);
			await port.incoming.schedule(when, port.startChange(segment));
		},
		cancelIncoming: (when) => port.incoming.stop(when),
		stopOutgoing: async (when) => {
			outgoing = rt.processor;
			if (outgoing !== null) await outgoing.stop(when);
		},
		restoreOutgoing: async (at, segment) => {
			if (outgoing !== null) await outgoing.schedule(at, port.startChange(segment));
		},
		retireOutgoingAfter: (when, tailSec) => {
			const retiring = outgoing;
			if (retiring === null) return;
			// Retired once its stop has rendered, never while it is the audible tail.
			const delaySec = Math.max(0, when - port.clock.currentTime) + tailSec;
			setTimer(() => port.retire(retiring), delaySec * 1000);
		},
		cutOutgoingAt: (when, stillPending) => {
			const cutting = outgoing;
			let fired = false;
			if (cutting !== null) {
				setTimer(() => {
					if (!stillPending()) return;
					fired = true;
					port.retire(cutting);
				}, Math.max(0, when - port.clock.currentTime) * 1000);
			}
			return () => fired;
		},
		reportOutgoingFailure: (error) => port.outgoingFailed(error, false),
		failOutgoing: (error) => {
			if (outgoing !== null && rt.processor === outgoing) port.outgoingFailed(error, true);
		},
		commit: (when) => port.commit(when),
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
