/**
 * PARITY-10: resync Beat Sync after a deferred beatgrid upgrade lands.
 *
 * Split out of audio-engine.svelte.ts (T4: that file sits at the
 * file_size.max_frontend ratchet floor) rather than inlined, so the ceiling
 * on one already-maximal file does not block a correctness fix.
 *
 * Two decks can each still be mid-request when either one's grid lands, so
 * both directions of that race need a path: if the landed grid belongs to
 * the sync MASTER, retry every follower whose earlier attempt found this
 * master gridless and was left alone rather than disabled (see the pending
 * check in `resyncAfterBeatgridUpgrade`). If the landed grid belongs to a
 * FOLLOWER and the master is still gridless, that is a PENDING state, not a
 * failure - leave beat_sync_enabled exactly as the DJ set it and let the
 * master's own landing retry this deck via the branch above, instead of
 * disabling it here on a race it cannot control. But a PENDING state is only
 * ever resolved by the master's own beatgrid upgrade SETTLING - if that
 * settles WITHOUT a grid (rekordbox-mapped, no analysis row, swapped away),
 * no further landing will ever come for these followers to wait on, so
 * `reconcileAfterBeatgridSettled` abandons them explicitly on that settlement
 * instead of leaving Beat Sync silently inert forever.
 *
 * The real _synchronizeFollowers plans each follower independently and does
 * NOT reject just because one of several followers cannot phase-lock (BAR
 * tempo bounds, etc) - it resolves normally, sets that deck's sync_error,
 * and reports its own toast, so the rest of the batch stays locked. It only
 * ever rejects for a genuinely batch-wide cause (every follower failed to
 * plan, or the scheduling stage itself failed). Either way, sync_error is
 * the one signal that tells the caller EXACTLY which decks in the batch did
 * not end up phase-locked - resolve and reject both leave it set correctly
 * per deck - so reconciliation reads that per deck rather than trusting the
 * promise's settlement to mean "all" or "none".
 */
import type { DeckState } from '$lib/rb/deck-state-types';

type DeckId = DeckState['deck_id'];

/** The engine surface this module needs, injected so it never depends on
 * audio-engine.svelte.ts's private module state directly. */
export interface BeatgridResyncPorts {
	deckIds: readonly DeckId[];
	syncMaster(): DeckId | null;
	playing(deck: DeckId): boolean;
	beatSyncEnabled(deck: DeckId): boolean;
	setBeatSyncEnabled(deck: DeckId, enabled: boolean): void;
	hasRealBeatGrid(deck: DeckId): boolean;
	hasSyncError(deck: DeckId): boolean;
	setSyncError(deck: DeckId, message: string): void;
	requiresReschedule(deck: DeckId, playing: boolean, enabled: boolean, master: DeckId): boolean;
	synchronizeFollowers(master: DeckId, followers: readonly DeckId[]): Promise<void>;
	hasSettledGridless(deck: DeckId): boolean;
	markSettledGridless(deck: DeckId): void;
	/** Record that `follower` wanted sync to `master` but `master` was gridless
	 * at the time - the ONLY followers a later handover reconciliation may
	 * ever touch, so a follower already phase-locked elsewhere (or one that
	 * simply never asked) can never be swept into someone else's settlement.
	 * EXCLUSIVE: a deck follows exactly one master, so recording it here also
	 * withdraws it from every other master's pending set. Re-marking is the
	 * normal path, not an edge case - an election hands the role to a second
	 * still-gridless deck and the follower's next grid adoption re-marks it
	 * against the new one. Without the withdrawal the old master's own
	 * settlement (or clear) still reaches a follower that has moved on and
	 * disables it for a race it is no longer part of, naming the wrong deck
	 * in the user-facing error (PR #765 'Move followers between pending
	 * masters'). */
	markPending(follower: DeckId, master: DeckId): void;
	/** Returns and clears every follower currently marked pending against
	 * `master`. */
	takePending(master: DeckId): readonly DeckId[];
	/** Non-mutating peek at whether `takePending(master)` would return anyone -
	 * lets a caller decide how wide a scope claim a settlement needs BEFORE
	 * draining the set for real (see `resyncSettlementNeedsFullBarrier`). */
	hasPendingFollowers(master: DeckId): boolean;
}

/** Disable Beat Sync for exactly the decks synchronizeFollowers left
 * unlocked, never the whole batch - a deck that phase-locked successfully
 * must not be punished for a batch-mate's failure. */
function _reconcileSyncErrors(ports: BeatgridResyncPorts, decks: readonly DeckId[]): void {
	for (const deck of decks) {
		if (ports.hasSyncError(deck)) ports.setBeatSyncEnabled(deck, false);
	}
}

function _reportPhaseLockFailure(
	ports: BeatgridResyncPorts,
	decks: readonly DeckId[],
	source: DeckId,
	error: unknown
): never {
	_reconcileSyncErrors(ports, decks);
	const message = error instanceof Error ? error.message : String(error);
	throw new Error(`deck ${source} beatgrid arrived but could not phase-lock (BAR): ${message}`, {
		cause: error instanceof Error ? error : undefined
	});
}

export function resyncAfterBeatgridUpgrade(ports: BeatgridResyncPorts, deck: DeckId): Promise<void> {
	const master = ports.syncMaster();
	if (master === null) return Promise.resolve();
	if (master === deck) {
		const followers = ports.deckIds.filter(
			(candidate) =>
				ports.requiresReschedule(
					candidate,
					ports.playing(candidate),
					ports.beatSyncEnabled(candidate),
					master
				) && ports.hasRealBeatGrid(candidate)
		);
		if (followers.length === 0) return Promise.resolve();
		return ports.synchronizeFollowers(master, followers).then(
			() => _reconcileSyncErrors(ports, followers),
			(error: unknown) => _reportPhaseLockFailure(ports, followers, deck, error)
		);
	}
	if (!ports.requiresReschedule(deck, ports.playing(deck), ports.beatSyncEnabled(deck), master)) {
		return Promise.resolve();
	}
	if (!ports.hasRealBeatGrid(master)) {
		if (ports.hasSettledGridless(master)) _disableForGridlessMaster(ports, deck, master);
		else ports.markPending(deck, master);
		return Promise.resolve();
	}
	return ports.synchronizeFollowers(master, [deck]).then(
		() => _reconcileSyncErrors(ports, [deck]),
		(error: unknown) => _reportPhaseLockFailure(ports, [deck], deck, error)
	);
}

/** Whether a settlement for `deck` (r3913096141 P1) needs the caller's
 * all-deck-plus-sync barrier, or can run claiming only `deck`'s own scope.
 *
 * The question is NOT "is a sync master elected" but "will THIS settlement
 * read or write another deck" (r3919411682 P1 BLOCKING: a master merely
 * existing made every settlement widen, so a stopped, follower-less deck
 * claimed every deck plus `sync` for provably no cross-deck work - the wide
 * claim then waited out an unrelated deck's slow load and became the master's
 * new tail, stalling a later control on that otherwise independent master).
 * Answered branch by branch, against what `reconcileAfterBeatgridSettled`
 * actually reaches:
 *
 * - Anyone pending on `deck` means `_reconcileStrandedFollowers` (or
 *   `_abandonStuckFollowersForGridlessMaster`) will relocate or abandon those
 *   OTHER decks, on either outcome and with or without a master
 *   (r3913350822's null-master branch abandons them too). Always wide.
 * - Otherwise a GRIDLESS settlement is `markSettledGridless(deck)` plus a
 *   drain of an empty pending set. Both are keyed on `deck` alone and both are
 *   synchronous, so no other deck is read or written whoever holds the master
 *   role. Narrow.
 * - Otherwise a LANDED settlement runs `resyncAfterBeatgridUpgrade`, which
 *   returns immediately with no master; scans every deck for followers to
 *   reschedule when `deck` IS the master; and when the master is a DIFFERENT
 *   deck, reads that master (and can phase-lock to it) only once
 *   `requiresReschedule` says `deck` genuinely wants to follow it - which a
 *   stopped deck, or one with Beat Sync off, never does.
 *
 * `landed` is therefore load-bearing and cannot be dropped again: it is what
 * separates the two outcomes' reachability once nothing is pending. It does
 * NOT change the answer in the pending or null-master cases, which is what
 * the earlier no-`landed` signature was really recording.
 *
 * MUST be called fresh at the moment a settlement's task actually begins
 * running, never before scheduling it: `deck`'s own scope claim can queue
 * behind an unrelated in-flight command (e.g. PLAY electing `deck` as
 * master) that changes this answer between when the settlement was
 * scheduled and when it starts (r3913350814 P1) - see
 * scoped-sync-runner.ts's `widen` parameter, which lets the caller recheck
 * here and escalate only if this now reports true. Nothing can interleave
 * between this call and the settlement body it gates: the caller invokes the
 * body synchronously on the narrow path, and every narrow branch above
 * completes without awaiting. */
export function resyncSettlementNeedsFullBarrier(
	ports: BeatgridResyncPorts,
	deck: DeckId,
	landed: boolean
): boolean {
	if (typeof landed !== 'boolean') {
		throw new TypeError('beatgrid settlement landed flag must be boolean');
	}
	if (ports.hasPendingFollowers(deck)) return true;
	if (!landed) return false;
	const master = ports.syncMaster();
	if (master === null) return false;
	if (master === deck) return true;
	return ports.requiresReschedule(deck, ports.playing(deck), ports.beatSyncEnabled(deck), master);
}

function _disableForGridlessMaster(ports: BeatgridResyncPorts, follower: DeckId, master: DeckId): void {
	ports.setSyncError(follower, `deck ${master} settled without a beatgrid - Beat Sync cannot phase-lock`);
	ports.setBeatSyncEnabled(follower, false);
}

/** `stranded` is captured synchronously by the caller, well before this
 * deferred abandonment actually runs - a DJ can switch Beat Sync off for one
 * of these followers in that gap. `setBeatSync(follower, false)` clears the
 * follower's OWN pendingByMaster membership, but this captured array lives
 * outside that tracking, so without this recheck the abandonment still lands
 * and re-brands an opted-out deck with a sync_error for a wait it withdrew
 * from (discussion_r3920394940). */
function _abandonStrandedFollowers(ports: BeatgridResyncPorts, master: DeckId, stranded: readonly DeckId[]): void {
	for (const follower of stranded) {
		if (ports.beatSyncEnabled(follower)) _disableForGridlessMaster(ports, follower, master);
	}
}

/** Phase-lock exactly the given (already-known-stranded) followers to
 * `master` - never recompute the full "everyone who currently wants sync"
 * set here, or a handover reconciliation would resync an unrelated, already
 * phase-locked follower playing on the same master and risk disrupting its
 * running transport schedule for no reason (see the takePending contract
 * above). `master` may itself appear in a stale `stranded` list (it was
 * marked pending before it was elected), so this still filters through
 * requiresReschedule/hasRealBeatGrid to drop anyone no longer eligible. */
function _syncStrandedFollowers(ports: BeatgridResyncPorts, master: DeckId, stranded: readonly DeckId[]): Promise<void> {
	const followers = stranded.filter(
		(candidate) =>
			ports.requiresReschedule(candidate, ports.playing(candidate), ports.beatSyncEnabled(candidate), master) &&
			ports.hasRealBeatGrid(candidate)
	);
	if (followers.length === 0) return Promise.resolve();
	return ports.synchronizeFollowers(master, followers).then(
		() => _reconcileSyncErrors(ports, followers),
		(error: unknown) => _reportPhaseLockFailure(ports, followers, master, error)
	);
}

/** The master settled without ever landing a grid, so no future landing will
 * retry the followers a prior `resyncAfterBeatgridUpgrade` call left pending
 * against it (see the module docstring). `stranded` must be exactly
 * `ports.takePending(master)`, captured by the caller - this is the ONLY set
 * this function may ever touch, never a scan over every deck (r3913693394 P1
 * BLOCKING: a broad `deckIds` scan swept in decks that had Beat Sync enabled
 * by default and a grid loaded but never once tried to follow this master,
 * disabling them for a race they were never part of). Within that set, gated
 * on `beatSyncEnabled` alone, NOT `requiresReschedule`'s live `playing`
 * state: a follower can mark pending while playing, then get paused before
 * this settlement runs, and this abandonment is its ONLY remaining chance to
 * ever get disabled. Re-checking `playing` here would silently skip it, so a
 * later PLAY would walk straight into a reject against a still-gridless
 * master without ever flipping Beat Sync off (issue #734 send-back
 * r3912757828). */
function _abandonStuckFollowersForGridlessMaster(
	ports: BeatgridResyncPorts,
	master: DeckId,
	stranded: readonly DeckId[]
): void {
	for (const follower of stranded) {
		if (ports.beatSyncEnabled(follower) && ports.hasRealBeatGrid(follower)) {
			_disableForGridlessMaster(ports, follower, master);
		}
	}
}

/** Reconciles every follower `deck` itself left stranded in `pendingByMaster`
 * against whoever the sync master actually is NOW - used both when `deck`
 * settles gridless (it will never retry them) and when `deck`'s own request
 * later LANDS a grid while `deck` is no longer master (its landing does
 * nothing for followers pending on a role it already lost - issue #734
 * send-back Finding 1), AND when `deck` is cleared/unloaded before its own
 * settlement ever ran (reconcileBeforeClear). A NULL master here is not the
 * same "nothing to do" as an empty `stranded` list: these followers were
 * stranded specifically because `deck` (gridless or now gone) will never
 * retry them, and no OTHER settlement event is coming to abandon them either
 * - the dedicated no-master branch in `reconcileAfterBeatgridSettled` only
 * covers deck's own settlement, not this pre-clear path, so a follower left
 * pending here would stay Beat-Sync-enabled with no master at all until its
 * own next PLAY rejects trying to phase-lock against nothing (r3913350822).
 * Always drains `takePending(deck)` once a master exists, even when `deck`
 * still holds the role: that case's stranded set is already a subset of
 * whatever a master-branch resync recomputes from scratch, so nothing
 * further needs doing here besides the drain. */
function _reconcileStrandedFollowers(
	ports: BeatgridResyncPorts,
	deck: DeckId,
	stranded: readonly DeckId[]
): Promise<void> {
	if (stranded.length === 0) return Promise.resolve();
	const master = ports.syncMaster();
	if (master === deck) return Promise.resolve();
	if (master === null) {
		_abandonStrandedFollowers(ports, deck, stranded);
		return Promise.resolve();
	}
	if (ports.hasRealBeatGrid(master)) {
		return _syncStrandedFollowers(ports, master, stranded);
	}
	if (ports.hasSettledGridless(master)) {
		// The new master already gave up on its own grid on some earlier
		// settlement, before this handover - nothing will ever call this
		// function for it again, so these followers must be abandoned now
		// rather than re-marked pending on a master that will never retry.
		_abandonStrandedFollowers(ports, master, stranded);
		return Promise.resolve();
	}
	for (const follower of stranded) ports.markPending(follower, master);
	return Promise.resolve();
}

/** Fired before a deck's loaded-track state is wiped (a fresh load replacing
 * it, or a processor failure) - the deck's own in-flight beatgrid request is
 * about to be invalidated and will never reach either settlement path (it is
 * not a landing and not a gridless settlement, just gone), so any followers
 * left pending on it must be reconciled against whoever is master NOW before
 * that record is lost for good (issue #734 send-back Finding 3: a follower
 * left pending against a master that gets replaced, rather than settling,
 * would otherwise stay Beat-Sync-enabled without ever phase-locking to the
 * deck that took over the role). `stranded` must be `ports.takePending(deck)`
 * captured synchronously by the CALLER, before clearForDeck (or anything else
 * that can race a deferred continuation) discards the same pending record -
 * this function never drains the port itself, because a caller that defers
 * this call behind a scoped-command claim (audio-engine.svelte.ts's
 * `_clearLoadedTrackState`, wrapped in `_scopedSync.run`) would otherwise have
 * its own synchronous clearForDeck wipe the pending set before this function's
 * drain ever ran, silently discarding every stranded follower (issue #734
 * send-back r3912339491, second pass). Reuses the exact relocate-or-sync-or-
 * abandon logic a genuine settlement already runs, so a follower that is
 * itself the newly elected master is excluded the same way requiresReschedule
 * already excludes a deck from following itself. */
export function reconcileBeforeClear(
	ports: BeatgridResyncPorts,
	deck: DeckId,
	stranded: readonly DeckId[]
): Promise<void> {
	return _reconcileStrandedFollowers(ports, deck, stranded);
}

/** Single entry point for a beatgrid upgrade's settlement, landed or not -
 * see beatgrid-upgrade.ts's `onSettled`. Callers no longer branch on `landed`
 * themselves; this module owns both outcomes of the same race.
 *
 * The gridless mark is recorded for EVERY deck that settles without a grid,
 * not only one that is master at the moment it settles: a deck can settle
 * gridless while stopped with no master elected, then later become master
 * itself once it starts.
 *
 * A settling deck may no longer BE the master by the time this runs - a
 * master election can land between this deck's request going out and it
 * settling here, handing the role to a different deck that was itself one
 * of the pending followers. Gating the sweep on `syncMaster() === deck`
 * would then skip it entirely and strand every follower still pending
 * against the deck that just lost the role (issue #734 send-back P1). The
 * same gap exists on the LANDED side of this race: a deck's own grid can
 * land after it has already lost the master role, and resyncAfterBeatgridUpgrade
 * does nothing for followers pending on a role `deck` no longer holds - so
 * both outcomes funnel through `_reconcileStrandedFollowers` (Finding 1).
 *
 * The fix is NOT to blindly resync against whoever is master now - that
 * would resync EVERY follower currently wanting sync to that master,
 * including ones already correctly phase-locked there, and an unrelated
 * deck's own unmapped/unanalyzed load settling gridless would then disrupt
 * an established follower's running transport schedule for no reason (a
 * second P1 caught reviewing the first fix). So `takePending(deck)` names
 * the EXACT followers that were left pending specifically because `deck`
 * was gridless while they wanted to sync to it - that is the only set this
 * reconciliation may ever touch. If the current master already has a real
 * grid, those specific stranded followers get phase-locked to it. If the
 * current master is a *different*, still-gridless deck (another handover
 * mid-flight), they simply move to being pending against that new master
 * instead. Only when the current master IS the settling deck does this
 * fall back to abandoning its followers - a non-master deck settling has no
 * followers of its own to abandon (or reconcile) at all. */
export function reconcileAfterBeatgridSettled(
	ports: BeatgridResyncPorts,
	deck: DeckId,
	landed: boolean
): Promise<void> {
	if (landed) {
		// A rejection here (a genuine batch-wide phase-lock failure on deck's
		// OWN follower-sync) must not skip draining takePending(deck) - any
		// OTHER follower still stranded against the role deck may have already
		// lost needs reconciling against whoever the current master actually
		// is, or it is left Beat-Sync-enabled without ever phase-locking
		// (issue #734 send-back r3912654134). The original failure still
		// propagates once that reconciliation settles.
		return resyncAfterBeatgridUpgrade(ports, deck).then(
			() => _reconcileStrandedFollowers(ports, deck, ports.takePending(deck)),
			(error: unknown) =>
				_reconcileStrandedFollowers(ports, deck, ports.takePending(deck)).then(() => {
					throw error;
				})
		);
	}
	ports.markSettledGridless(deck);
	const master = ports.syncMaster();
	if (master === null) {
		// No sync master is active right now, but `deck` just confirmed it will
		// never grid - its own pending followers are not waiting on a role
		// deck currently holds, they are waiting on THIS settlement specifically,
		// so they must be abandoned now rather than left with a stale pending
		// record for a later re-election to silently inherit (issue #734
		// send-back r3912960716).
		for (const follower of ports.takePending(deck)) _disableForGridlessMaster(ports, follower, deck);
		return Promise.resolve();
	}
	if (master === deck) {
		_abandonStuckFollowersForGridlessMaster(ports, master, ports.takePending(deck));
		return Promise.resolve();
	}
	return _reconcileStrandedFollowers(ports, deck, ports.takePending(deck));
}

export { createBeatgridResyncTracking } from '$lib/player/beatgrid-resync-tracking';
