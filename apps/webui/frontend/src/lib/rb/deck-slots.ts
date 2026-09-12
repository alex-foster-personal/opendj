/**
 * Deck slot addressing: which of the four physical slots, and how one is
 * chosen for a load gesture.
 *
 * Split out of the former lib/rb/types.ts god module. `DeckId` is the single
 * most widely shared primitive in the performance UI, so it owns a module
 * rather than riding along with a payload shape. The double-click picker
 * lives here because it is the arbitration policy OVER those slots - same
 * concept, and it keeps BrowserPanel.svelte importing one module for both.
 */

export type { DeckId } from './deck-id';
import type { DeckId } from './deck-id';

/** Everything the picker needs about one slot to rank it as a victim. */
export interface DeckSlotState {
  stable_id: string | null;
  playing: boolean;
  is_master: boolean;
  /** Channel fader 0..1. A deck at 0 is loaded but inaudible. */
  fader: number;
  /** Monotonic load counter; higher = loaded more recently. */
  loadSeq: number;
  /**
   * True from the moment this deck is reserved (_reserveDeckSlot) until its
   * load/unload actually settles or is released. Distinct from loadSeq
   * ordering, which only says WHEN a deck was last reserved relative to
   * others - it cannot say whether that reservation already settled a
   * while ago (legitimately idle now) or is still in flight (must not be
   * handed to a second gesture). A tier's own field alone cannot see this:
   * stable_id/playing/fader do not change until the reservation settles.
   */
  reservationPending: boolean;
}

export interface DoubleClickPickInput {
  shift: boolean;
  replace: boolean;
  decks: Record<DeckId, DeckSlotState>;
  lastDoubleClickDeck?: DeckId | null;
}

export type DoubleClickPickResult =
  { deck: DeckId; error?: undefined } | { deck: null; error: string | null };

/**
 * Least-recently-reserved of a tier's candidates, walking `candidates` in
 * order so a tie (equal loadSeq - the common case, nothing reserved) keeps
 * the 1,2,3,4 preference. Every idleness tier below needs this, not just the
 * empty one: `_reserveDeckSlot` bumps loadSeq synchronously, before any of
 * stable_id/playing/fader change, so a deck reserved a moment ago for an
 * in-flight load can still read as empty, stopped, AND silent - whichever
 * tier a second double-click lands in first, it must skip that deck the same
 * way.
 */
function _leastRecentlyReserved(
  candidates: DeckId[],
  decks: Record<DeckId, DeckSlotState>,
): DeckId {
  let victim = candidates[0];
  for (const d of candidates) {
    if (decks[d].loadSeq < decks[victim].loadSeq) victim = d;
  }
  return victim;
}

/**
 * Smart-load target.
 *
 * Ranked by how little it costs to take the slot, then by the maintainer's 1,2,3,4
 * preference within a tier: empty, then stopped, then loaded-but-faded-out,
 * then the stalest of what is left. Shift restricts the search to CH3/CH4.
 * Replace (cmd/ctrl) reuses lastDoubleClickDeck instead of advancing, and
 * falls through to the normal pick if nothing was double-clicked yet.
 *
 * THE MASTER IS NEVER A VICTIM. It used to be reachable - the old rule read
 * only deckLoadSeq across CH1/CH2, so master and playing were invisible to it
 * and the least-recently-loaded deck won even while it was feeding the
 * speakers. Pin d2c156a503bb is exactly that: a double-click replaced the
 * live master mid-set. Nothing else this gesture can do is as destructive.
 *
 * Callers MUST reserve the returned deck (bump deckLoadSeq) synchronously,
 * before starting the async load - see _reserveDeckSlot in BrowserPanel.
 * Regression Mon 17 Aug 2026: reserving only after the load resolved let two
 * fast double-clicks both read the same stale deckLoadSeq and pick the same
 * deck, so the second track silently replaced the first instead of landing
 * on the other deck.
 */
export function pickDoubleClickDeck(
  input: DoubleClickPickInput,
): DoubleClickPickResult {
  const decks = input.decks;
  if (input.replace && input.lastDoubleClickDeck != null) {
    const cached = input.lastDoubleClickDeck;
    // Only reuse the cached deck while it is still an eligible target. It
    // can have become master since the plain double-click that cached it -
    // e.g. an A/B swap - and returning it unconditionally here would bypass
    // the master exclusion below entirely, cutting the live output despite
    // "the master is never a victim". Falling through re-runs the normal
    // pick, which still honors master exclusion and idleness tiers.
    if (!decks[cached].is_master) {
      return { deck: cached };
    }
  }

  const order: DeckId[] = input.shift ? [3, 4] : [1, 2, 3, 4];
  // The master is never a victim. Even when it is the stalest, quietest deck
  // on the board it is the one currently feeding the speakers, and taking it
  // is the single most destructive thing this gesture could do (pin
  // d2c156a503bb). TODO: the maintainer flagged one exception worth handling later -
  // a master whose own state is stale - deliberately not guessed at here.
  const usable = order.filter((d) => !decks[d].is_master);
  if (usable.length === 0) {
    return {
      deck: null,
      error: input.shift
        ? "shift+dblclick: CH3 and CH4 are both master - unload one first"
        : "every deck is master - unload one first",
    };
  }

  // A pending reservation must be excluded ACROSS every tier, not just
  // ordered within one: if CH1 is the only empty deck and was reserved a
  // moment ago for a load that has not settled, tier priority alone always
  // hands it straight back out (an empty singleton always wins its tier,
  // so within-tier loadSeq ordering never gets a second candidate to
  // compare against). Compute the pending-free pool ONCE, before any tier
  // runs, so a reserved-but-unsettled CH1 cannot shadow an untouched,
  // merely-stopped CH2. Falls back to the full `usable` set only when every
  // non-master deck is pending (e.g. three decks double-clicked in a fast
  // burst) - refusing the gesture entirely would be worse.
  const free = usable.filter((d) => !decks[d].reservationPending);
  const candidates = free.length > 0 ? free : usable;

  // Idleness tiers, best victim first. Within a tier the 1,2,3,4 preference
  // decides at equal loadSeq, which is why `order` is walked rather than
  // sorted. Each tier's own field (stable_id/playing/fader) cannot see a
  // reservation still in flight - _reserveDeckSlot bumps loadSeq
  // synchronously, before any of those fields change - so every tier also
  // prefers the least-recently-reserved candidate among what is left after
  // the pending exclusion above, the same staleness signal the final
  // busy-board tier below already used.
  const empty = candidates.filter((d) => decks[d].stable_id === null);
  if (empty.length > 0) return { deck: _leastRecentlyReserved(empty, decks) };
  const stopped = candidates.filter((d) => !decks[d].playing);
  if (stopped.length > 0)
    return { deck: _leastRecentlyReserved(stopped, decks) };
  const silent = candidates.filter((d) => decks[d].fader <= 0);
  if (silent.length > 0) return { deck: _leastRecentlyReserved(silent, decks) };

  // Shift explicitly TARGETS the pair decks, so when both are playing there
  // is no other slot the user was willing to accept: refusing is right, and
  // this refusal predates the pin (it is what stops shift stealing audio).
  // The unshifted gesture has no such constraint, so it degrades instead.
  if (input.shift) {
    return {
      deck: null,
      error:
        "shift+dblclick: CH3 and CH4 are both playing - pause or unload one first",
    };
  }

  // Everything is playing and audible. Take the stalest rather than
  // refusing: a gesture that stops working when the board is full is worse
  // than one that picks the least-recently-touched deck.
  return { deck: _leastRecentlyReserved(candidates, decks) };
}

/**
 * `?extroute=1:1,2:7` -- address deck slots to external USB output pairs
 * (deck:usbLeft). Lives with slot addressing rather than in the engine
 * (convention D5: the engine keeps call sites, not parsers). Throws on any
 * malformed segment: a half-applied routing would silently send a deck to the
 * wrong soundcard channel.
 */
export function parseExternalRouting(): Map<DeckId, number> | null {
  if (typeof window === "undefined") return null;
  const raw = new URLSearchParams(window.location.search).get("extroute");
  if (raw === null || raw === "") return null;
  const routing = new Map<DeckId, number>();
  for (const part of raw.split(",")) {
    const match = part.match(/^([1-4]):(\d{1,2})$/);
    if (match === null) {
      throw new Error(
        `extroute: bad segment '${part}' (want deck:usbLeft, e.g. 1:1,2:7)`,
      );
    }
    const deck = Number(match[1]) as DeckId;
    const usbLeft = Number(match[2]);
    if (usbLeft < 1)
      throw new Error(`extroute: usbLeft must be >= 1 in '${part}'`);
    if (routing.has(deck))
      throw new Error(`extroute: deck ${deck} routed twice`);
    routing.set(deck, usbLeft);
  }
  return routing;
}

// ---------------------------------------------------------------------------
// Pending load-play intent (pin 9b8d55)
//
// Space pressed while a track is still decoding must be remembered, not
// dropped. Generation numbers make that safe: a stale intent from a load the
// user has already superseded can never resurrect and play the wrong track.
//
// This lives in deck-slots rather than a module of its own because every
// consumer (BrowserPanel, performance-ipc, performance-hotkeys) already
// depends on deck-slots for DeckId, and all access is imperative, so it needs
// no rune and earns no new import edge.
// ---------------------------------------------------------------------------
export interface PendingLoadPlay {
	deck: DeckId;
	generation: number;
	desiredPlay: boolean;
	/**
	 * Q1: the operator's press, carried ACROSS the load it is waiting on.
	 *
	 * Space on a still-decoding track, and a direct Load+Play on one (e.g. a
	 * TrackTable double-click), are both supported gestures, and the wait the
	 * operator feels starts at that keydown or click, not at the play command
	 * the load completion eventually dispatches. Without this the row starts
	 * timing after the load and understates the felt latency by the whole
	 * decode.
	 *
	 * The resulting `press_to_schedule_ms` therefore legitimately spans a deck
	 * load and can be seconds. That is the honest number for THIS gesture and
	 * a wrong one for S2's 30ms budget (`specs/perf-latency-program.md` S2), so
	 * the row must say so itself rather than leaving an S2 consumer to guess
	 * from duration - see `markLoadSpanningPress` below, spent by
	 * `press-stamp.ts` into the row's own `kind`.
	 */
	pressT0Ms?: number;
}

let nextGeneration = 0;
let pendingByDeck: Record<DeckId, PendingLoadPlay | null> = {
	1: null,
	2: null,
	3: null,
	4: null
};

export function beginPendingLoadPlay(deck: DeckId, desiredPlay: boolean): PendingLoadPlay {
	nextGeneration += 1;
	const pending = { deck, generation: nextGeneration, desiredPlay };
	pendingByDeck = { ...pendingByDeck, [deck]: pending };
	return pending;
}

export function setPendingLoadPlayIntent(
	deck: DeckId,
	generation: number,
	desiredPlay: boolean,
	pressT0Ms?: number
): boolean {
	const pending = pendingByDeck[deck];
	if (pending === null || pending.generation !== generation) return false;
	pendingByDeck = {
		...pendingByDeck,
		[deck]: {
			...pending,
			desiredPlay,
			...(pressT0Ms === undefined ? {} : { pressT0Ms })
		}
	};
	return true;
}

export function consumePendingLoadPlay(deck: DeckId, generation: number): PendingLoadPlay | null {
	const pending = pendingByDeck[deck];
	if (pending === null || pending.generation !== generation) return null;
	return { ...pending };
}

export function clearPendingLoadPlay(deck: DeckId, generation: number): boolean {
	const pending = pendingByDeck[deck];
	if (pending === null || pending.generation !== generation) return false;
	pendingByDeck = { ...pendingByDeck, [deck]: null };
	return true;
}

export function mostRecentPendingLoadPlay(): PendingLoadPlay | null {
	let mostRecent: PendingLoadPlay | null = null;
	for (const pending of Object.values(pendingByDeck)) {
		if (pending !== null && (mostRecent === null || pending.generation > mostRecent.generation)) {
			mostRecent = pending;
		}
	}
	return mostRecent === null ? null : { ...mostRecent };
}

export function pendingLoadPlayState(): Record<DeckId, PendingLoadPlay | null> {
	return {
		1: pendingByDeck[1] === null ? null : { ...pendingByDeck[1] },
		2: pendingByDeck[2] === null ? null : { ...pendingByDeck[2] },
		3: pendingByDeck[3] === null ? null : { ...pendingByDeck[3] },
		4: pendingByDeck[4] === null ? null : { ...pendingByDeck[4] }
	};
}

// ---------------------------------------------------------------------------
// Load-spanning press classification (P1 BLOCKING, r3974057968 follow-up)
//
// A press stamp handed to `dispatchPerformanceCommand` after riding through
// `PendingLoadPlay.pressT0Ms` always spans this deck's load: the deferred
// play is dispatched only once the awaited `load` command settles, so the
// eventual schedule's `press_to_schedule_ms` covers the decode, not just the
// scheduler queue. Marked here, at press-stamp.ts's request, so the ring row
// can say so in its own `kind` (see press-audible.ts's
// PRESS_SCHEDULE_LOAD_SPAN_KIND) instead of an aggregator guessing from
// duration. A plain Map keyed by the stamp itself, not by deck/generation:
// press-stamp.ts never sees either, only the same `pressT0Ms` number that
// already threads all the way to `_scheduleDeckSerial`.
// ---------------------------------------------------------------------------
const loadSpanningPressStamps = new Set<number>();

/** Call once, right before spending a `PendingLoadPlay.pressT0Ms`. */
export function markLoadSpanningPress(pressT0Ms: number): void {
	loadSpanningPressStamps.add(pressT0Ms);
}

/** Consume-once: a stamp answers `true` for its one schedule, never again. */
export function claimLoadSpanningPress(pressT0Ms: number | undefined): boolean {
	if (pressT0Ms === undefined) return false;
	return loadSpanningPressStamps.delete(pressT0Ms);
}

// ---------------------------------------------------------------------------
// Armed hot-cue press classification (P1 BLOCKING, PRRT_kwDOSEvNd86g96Le)
//
// A BeatSyncMax hot-cue trigger that `planHotCueTrigger` decides to arm waits
// for the next downbeat before its schedule fires - a multi-beat wait an
// aggregator must not mistake for an ordinary Class A schedule delay. Marked
// at `performance-ipc.svelte.ts`'s `hot_cue_trigger` dispatch, right before
// `_hotCueDriver.arm`, and claimed here by the same stamp so the ring row can
// say so in its own `kind` (press-audible.ts's PRESS_SCHEDULE_ARMED_KIND).
// ---------------------------------------------------------------------------
const armedHotCuePressStamps = new Set<number>();

/** Call once, right before arming a deferred hot-cue trigger. */
export function markArmedHotCuePress(pressT0Ms: number): void {
	armedHotCuePressStamps.add(pressT0Ms);
}

/** Consume-once: a stamp answers `true` for its one schedule, never again. */
export function claimArmedHotCuePress(pressT0Ms: number | undefined): boolean {
	if (pressT0Ms === undefined) return false;
	return armedHotCuePressStamps.delete(pressT0Ms);
}
