/**
 * Continuous phase lock for the Web Audio engine's Beat Sync followers (NAE-19).
 *
 * The decision is `rb/phase-lock.ts`, shared with Rust engine mode. This module
 * is only the Web Audio engine's bookkeeping around it, kept out of the hotspot
 * `audio-engine.svelte.ts`, which talks to it through ports:
 *
 * - `record` is called where `_synchronizeFollowers` commits a plan. It stores
 *   the BASE tempo the join chose, the master and the master tempo it assumed,
 *   and the deck's load identity. Every trim is relative to that base, so trims
 *   never accumulate. `clear` is called at the top of every sync for its
 *   followers, so no trim can land between a join's plan and its own lock.
 * - `tick` runs from the engine's existing presentation frame (`_tick`,
 *   requestAnimationFrame), throttled here to `PHASE_LOCK_WEBAUDIO_INTERVAL_SEC`
 *   of AudioContext time. Positions are the engine's own schedule projection
 *   on the audio clock at one shared context time, not the presented UI mirror.
 * - A lock is DROPPED (never trimmed again) once anything it assumed stops holding:
 *   another master, Beat Sync off or the master role lost (`ownsTempo`), the
 *   deck stopped, another track or load, the master's tempo moved, or the
 *   follower's tempo was written by anything but this lock (a re-sync, a ramp).
 *   A user tempo action on a playing follower is refused by the engine while
 *   Beat Sync owns it, and every other tempo writer shows up in that check.
 *   A lock dropped mid-trim schedules its base back once (`_release`) when
 *   the deck still plays that trim and is not now the master.
 * - A lock is SKIPPED (kept, not trimmed) while either deck's schedule is not
 *   settled: queued or pending revisions, an unpresented revision, a re-anchor
 *   ramp in force, an armed quantized launch. A trim therefore never races a
 *   scheduled revision, and a trim's own revision has to be presented before
 *   the next trim is considered.
 * - A trim goes through the same tempo-only schedule `setTempoRatio` uses
 *   (`scheduleTempo`). A `reseek` re-runs the join (`resync`), which seeks.
 * - The lock carries what the decision needs between ticks: the base (which
 *   FOLLOWS the local tempo on an uneven grid, see `rb/phase-lock.ts`), how
 *   long ago the join was and how many ticks the error has been past the
 *   re-join line (a re-join is confirmed, and never sooner than
 *   `PHASE_LOCK_REJOIN_MIN_INTERVAL_SEC` after a join), and the offset the DJ
 *   dialed in (`nudge`), which the lock holds instead of correcting.
 */
import type { AnlzBeat } from '$lib/rb/anlz-types';
import type { TempoNormalization } from '$lib/rb/beat-sync-math';
import type { DeckId } from '$lib/rb/deck-id';
import { phaseLockDecision, phaseLockFeedForwardBase, phaseLockShouldSend, type PhaseLockDecision } from '$lib/rb/phase-lock';

/** At most one phase-lock evaluation per this much AudioContext time (30 Hz),
 * however fast the display's frame rate drives the presentation tick. */
export const PHASE_LOCK_WEBAUDIO_INTERVAL_SEC = 1 / 30;

/** Relative tolerance for "the tempo is still the one this lock wrote". The
 * engine carries tempo ratios through unchanged, so this only absorbs float
 * noise; a real tempo write is many orders of magnitude larger. */
const TEMPO_EQUAL_RTOL = 1e-9;

export type PhaseLockDeckId = DeckId;

/** What the Web Audio engine exposes to the lock. All reads are LIVE state. */
export interface WebAudioPhaseLockPorts {
	deckIds: readonly PhaseLockDeckId[];
	/** The playing sync master, or null. */
	syncMaster(): PhaseLockDeckId | null;
	/** The deck holding the master role, playing or not (`_ownedMaster`). */
	masterDeck(): PhaseLockDeckId | null;
	/** Beat Sync currently owns this deck's tempo (`_syncOwnsFollowerTempo`). */
	ownsTempo(deck: PhaseLockDeckId): boolean;
	playing(deck: PhaseLockDeckId): boolean;
	loadToken(deck: PhaseLockDeckId): number;
	stableId(deck: PhaseLockDeckId): string | null;
	/** No queued/pending/unpresented revision, no re-anchor ramp, no armed launch. */
	settled(deck: PhaseLockDeckId): boolean;
	/** The tempo the deck's schedule is heading to (latest pending, else control). */
	desiredTempo(deck: PhaseLockDeckId): number;
	beats(deck: PhaseLockDeckId): readonly AnlzBeat[];
	/** Projected transport position on the audio clock at `contextTime`. */
	positionSec(deck: PhaseLockDeckId, contextTime: number): number;
	pitchRangePct(deck: PhaseLockDeckId): number;
	loopEngaged(deck: PhaseLockDeckId): boolean;
	/** Tempo-only revision through the engine's schedule path. */
	scheduleTempo(deck: PhaseLockDeckId, ratio: number): Promise<unknown>;
	/** Re-run the join for `deck` against `master` (it seeks). */
	resync(master: PhaseLockDeckId, deck: PhaseLockDeckId): Promise<unknown>;
	reportError(deck: PhaseLockDeckId, message: string): void;
}

export interface WebAudioPhaseLockJoin {
	master: PhaseLockDeckId;
	/** The master tempo ratio the join planned against. */
	masterTempo: number;
	/** The follower tempo ratio the join chose. */
	base: number;
	normalization: TempoNormalization;
}

export interface WebAudioPhaseLock {
	master: PhaseLockDeckId;
	masterTempo: number;
	base: number;
	normalization: TempoNormalization;
	loadToken: number;
	stableId: string | null;
	/** The tempo the deck was last scheduled at by the join or this lock. */
	sent: number;
	/** A trim is in flight; the next tick waits for it. */
	busy: boolean;
	/** AudioContext time of the first tick after the join; null before it. */
	joinedAtContextTime: number | null;
	/** Consecutive ticks the error has been past the re-join line. */
	overLineTicks: number;
	/** The phase offset the DJ dialed in since the join, wall-clock ms. */
	userOffsetMs: number;
}

/** Why a lock was dropped, or why it was left alone this tick. */
export type WebAudioPhaseLockStep =
	| { deck: PhaseLockDeckId; kind: 'dropped'; reason: string; released?: true }
	| { deck: PhaseLockDeckId; kind: 'waiting'; reason: 'busy' | 'unsettled' }
	| { deck: PhaseLockDeckId; kind: 'decided'; decision: PhaseLockDecision; sent: boolean };

function _sameTempo(a: number, b: number): boolean {
	return Math.abs(a - b) <= TEMPO_EQUAL_RTOL * Math.max(Math.abs(a), Math.abs(b));
}

function _message(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}

export function createWebAudioPhaseLock(ports: WebAudioPhaseLockPorts) {
	const locks = new Map<PhaseLockDeckId, WebAudioPhaseLock>();
	let lastTickContextTime: number | null = null;

	function record(deck: PhaseLockDeckId, join: WebAudioPhaseLockJoin): void {
		if (join.master === deck) {
			throw new RangeError(`phase lock: deck ${deck} cannot follow itself`);
		}
		if (!Number.isFinite(join.base) || join.base <= 0) {
			throw new RangeError(`phase lock: base tempo must be > 0, got ${join.base}`);
		}
		if (!Number.isFinite(join.masterTempo) || join.masterTempo <= 0) {
			throw new RangeError(`phase lock: master tempo must be > 0, got ${join.masterTempo}`);
		}
		locks.set(deck, {
			master: join.master,
			masterTempo: join.masterTempo,
			base: join.base,
			normalization: join.normalization,
			loadToken: ports.loadToken(deck),
			stableId: ports.stableId(deck),
			sent: join.base,
			busy: false,
			joinedAtContextTime: null,
			overLineTicks: 0,
			userOffsetMs: 0
		});
	}

	/**
	 * The DJ moved `deck` by `deltaMs` of wall clock against its master (jog,
	 * nudge; positive = ahead). The lock holds the deck there from now on. The
	 * caller moves the audio; this only stops the lock from undoing it.
	 */
	function nudge(deck: PhaseLockDeckId, deltaMs: number): void {
		if (!Number.isFinite(deltaMs)) {
			throw new RangeError(`phase lock: nudge must be finite, got ${deltaMs}`);
		}
		const lock = locks.get(deck);
		if (lock === undefined) throw new RangeError(`phase lock: deck ${deck} has no lock to nudge`);
		lock.userOffsetMs += deltaMs;
	}

	function clear(deck: PhaseLockDeckId): void {
		locks.delete(deck);
	}

	function clearAll(): void {
		locks.clear();
		lastTickContextTime = null;
	}

	/** Why `lock` no longer holds, or null when it does. */
	function _broken(deck: PhaseLockDeckId, lock: WebAudioPhaseLock, master: PhaseLockDeckId | null): string | null {
		if (master === null) return 'no playing master';
		if (master !== lock.master) return `master moved ${lock.master} -> ${master}`;
		if (!ports.ownsTempo(deck)) return 'Beat Sync no longer owns the tempo';
		if (!ports.playing(deck)) return 'follower stopped';
		if (ports.loadToken(deck) !== lock.loadToken || ports.stableId(deck) !== lock.stableId) {
			return 'follower track changed';
		}
		return null;
	}

	/**
	 * A lock dropped with its trim in force takes the trim with it ("a trim
	 * never outlives its error"): the base goes back through the same
	 * tempo-only schedule. Only for a deck still PLAYING the lock's load (the
	 * schedule path starts transport), not now the master (#1134: a promoted
	 * deck's tempo is the one its followers lock to), and still heading to
	 * the lock's own last write: a tempo anything else wrote is left alone.
	 */
	function _release(deck: PhaseLockDeckId, lock: WebAudioPhaseLock): boolean {
		if (lock.sent === lock.base || ports.masterDeck() === deck || !ports.playing(deck)) return false;
		if (ports.loadToken(deck) !== lock.loadToken || ports.stableId(deck) !== lock.stableId) return false;
		if (!_sameTempo(ports.desiredTempo(deck), lock.sent)) return false;
		void ports.scheduleTempo(deck, lock.base).catch((error: unknown) => {
			ports.reportError(deck, `phase lock release failed: ${_message(error)}`);
		});
		return true;
	}

	/** Superseded tempo, read only once both schedules are settled. */
	function _superseded(deck: PhaseLockDeckId, lock: WebAudioPhaseLock): string | null {
		if (!_sameTempo(ports.desiredTempo(lock.master), lock.masterTempo)) return 'master tempo moved';
		if (!_sameTempo(ports.desiredTempo(deck), lock.sent)) return 'follower tempo written elsewhere';
		return null;
	}

	function _trim(deck: PhaseLockDeckId, lock: WebAudioPhaseLock, ratio: number): void {
		lock.busy = true;
		void ports
			.scheduleTempo(deck, ratio)
			.then(
				() => {
					if (locks.get(deck) === lock) lock.sent = ratio;
				},
				(error: unknown) => {
					if (locks.get(deck) === lock) {
						locks.delete(deck);
						ports.reportError(deck, `phase lock trim failed: ${_message(error)}`);
					}
				}
			)
			.finally(() => {
				lock.busy = false;
			});
	}

	/**
	 * One evaluation at AudioContext time `contextTime`. Returns what it did per
	 * locked deck (empty when throttled), for tests and diagnostics.
	 */
	function tick(contextTime: number): WebAudioPhaseLockStep[] {
		if (locks.size === 0) return [];
		if (
			lastTickContextTime !== null &&
			contextTime >= lastTickContextTime &&
			contextTime - lastTickContextTime < PHASE_LOCK_WEBAUDIO_INTERVAL_SEC
		) {
			return [];
		}
		lastTickContextTime = contextTime;
		const steps: WebAudioPhaseLockStep[] = [];
		const master = ports.syncMaster();
		for (const deck of ports.deckIds) {
			const lock = locks.get(deck);
			if (lock === undefined) continue;
			if (lock.busy) {
				steps.push({ deck, kind: 'waiting', reason: 'busy' });
				continue;
			}
			const broken = _broken(deck, lock, master);
			if (broken !== null) {
				locks.delete(deck);
				steps.push({ deck, kind: 'dropped', reason: broken, ...(_release(deck, lock) ? { released: true as const } : {}) });
				continue;
			}
			if (!ports.settled(deck) || !ports.settled(lock.master)) {
				steps.push({ deck, kind: 'waiting', reason: 'unsettled' });
				continue;
			}
			const superseded = _superseded(deck, lock);
			if (superseded !== null) {
				locks.delete(deck);
				steps.push({ deck, kind: 'dropped', reason: superseded });
				continue;
			}
			if (lock.joinedAtContextTime === null) lock.joinedAtContextTime = contextTime;
			let decision: PhaseLockDecision;
			try {
				const input = {
					masterBeats: ports.beats(lock.master),
					masterPositionSec: ports.positionSec(lock.master, contextTime),
					masterTempo: lock.masterTempo,
					followerBeats: ports.beats(deck),
					followerPositionSec: ports.positionSec(deck, contextTime),
					followerBaseTempo: lock.base,
					normalization: lock.normalization,
					pitchRangePct: ports.pitchRangePct(deck),
					sinceJoinSec: contextTime - lock.joinedAtContextTime,
					overLineTicks: lock.overLineTicks,
					userOffsetMs: lock.userOffsetMs
				};
				const forwarded = phaseLockFeedForwardBase(input); // follows a grid tempo change (F4)
				decision = phaseLockDecision({
					...input,
					followerBaseTempo: forwarded,
					trimming: lock.sent !== lock.base
				});
			} catch (error) {
				// Inside the presentation frame: drop this lock and say why rather
				// than let one deck's bad input stop the playhead for every deck.
				locks.delete(deck);
				ports.reportError(deck, `phase lock: ${_message(error)}`);
				steps.push({ deck, kind: 'dropped', reason: _message(error) });
				continue;
			}
			// On an uneven grid the base follows the local tempo; every trim, the
			// release and "a trim is in force" are relative to the base now.
			lock.base = decision.base;
			lock.overLineTicks = decision.overLineTicks;
			if (decision.action === 'reseek') {
				// A follower in its own loop is the DJ's: it is not seeked out of it.
				if (ports.loopEngaged(deck)) {
					steps.push({ deck, kind: 'decided', decision, sent: false });
					continue;
				}
				// The join clears and re-records the lock; drop it now so no second
				// re-seek starts while the first is in flight.
				locks.delete(deck);
				const lockMaster = lock.master;
				void ports.resync(lockMaster, deck).catch((error: unknown) => {
					ports.reportError(deck, `phase lock lost: ${_message(error)}`);
				});
				steps.push({ deck, kind: 'decided', decision, sent: true });
				continue;
			}
			const send = phaseLockShouldSend(lock.sent, decision.tempo, lock.base);
			if (send) _trim(deck, lock, decision.tempo);
			steps.push({ deck, kind: 'decided', decision, sent: send });
		}
		return steps;
	}

	function snapshot(): ReadonlyMap<PhaseLockDeckId, Readonly<WebAudioPhaseLock>> {
		return new Map([...locks].map(([deck, lock]) => [deck, { ...lock }]));
	}

	return { record, clear, clearAll, nudge, tick, snapshot };
}
