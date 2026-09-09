/**
 * Identity and session guards wrapped around BeatgridResyncPorts.
 *
 * Both beatgrid-settlement entry points defer their reconciliation behind a
 * scoped-command claim, so every write they make lands an unbounded time after
 * the facts that justified it were read: the deck can be reloaded, unloaded,
 * or the whole route disposed and remounted in that gap. beatgrid-resync.ts
 * itself is deliberately free of any notion of deck runtimes or route
 * sessions, and audio-engine.svelte.ts is pinned at the file_size.max_frontend
 * ratchet floor with zero headroom, so the wrapping lives here: the engine
 * supplies the identity probes, this module decides what each port write is
 * still allowed to do.
 *
 * Requirements:
 *   ✔︎ ✅ 🎯 A settlement never writes onto a track that replaced the one it settled for.
 *     [if] a deck reloads while its settlement waits for a scope [then] every
 *       guarded port write is skipped, not just the entry check ⛔️
 *   ✔︎ ✅ 🎯 A pre-clear reconciliation identifies a follower by a NON-REUSABLE token.
 *     [if] the route is disposed and remounted and the same deck is loaded the
 *       same number of times [then] the captured follower still reads as gone ⛔️
 *   ✔︎ ✅ 🎯 An authoritative /anlz grid replaces a deck's merged fallback grid.
 *     [if] a vendor mapping lands and an ambient /anlz retry returns a real
 *       PQTZ grid [then] the engine's deck.anlz adopts it and re-reconciles ⛔️
 */
import type { BeatgridResyncPorts } from '$lib/player/beatgrid-resync';
import { sameBeatgrid } from '$lib/rb/beatgrid-fallback';
import { isScopedCommandInvalidated } from '$lib/rb/performance-command-scheduler';
import { createScopedSyncRunner, type ScopedSyncRunner } from '$lib/player/scoped-sync-runner';
export { createBeatgridResyncTracking } from '$lib/player/beatgrid-resync-tracking';
export type { BeatgridResyncPorts } from '$lib/player/beatgrid-resync';
import type { AnlzData } from '$lib/rb/anlz-types';
import type { DeckState } from '$lib/rb/deck-state-types';

type DeckId = DeckState['deck_id'];

let _resyncModule: Promise<typeof import('$lib/player/beatgrid-resync')> | undefined;

function _loadResyncModule(): Promise<typeof import('$lib/player/beatgrid-resync')> {
	_resyncModule ??= import('$lib/player/beatgrid-resync');
	return _resyncModule;
}

/** Everything this module needs from the engine, as probes rather than state.
 *
 * A deck's load identity is deliberately THREE probes, not one number.
 * `deckLoadToken` alone distinguishes reloads within one route session but
 * repeats across sessions, because dispose() installs a fresh runtime per deck
 * whose counter restarts at 0. `deckRuntime` returns that runtime object
 * itself, which dispose() replaces and can never hand out again. `deckStableId`
 * names the published track, because load() increments its token before the
 * old state is cleared and publishes the replacement without a second token
 * change. Together the three probes are non-reusable in the way `beforeClear`
 * needs (discussion_r3919692507, discussion_r3922013754). */
export interface BeatgridResyncGuardDeps {
	ports: BeatgridResyncPorts;
	deckRuntime: (deck: DeckId) => object;
	deckLoadToken: (deck: DeckId) => number;
	deckStableId: (deck: DeckId) => string | null;
	deckAnlz: (deck: DeckId) => AnlzData | null;
	publishDeckAnlz: (deck: DeckId, anlz: AnlzData) => void;
	reportError: (message: string) => void;
}

function _message(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}

/** Rejection handler for a fire-and-forget claim. A route unmount invalidates
 * the command session, and every claim still queued behind it rejects with
 * ScopedCommandInvalidatedError - that is the scheduler working, not a
 * failure, and it must not surface as a toast (nor, unhandled, as an
 * unhandled promise rejection on ordinary navigation,
 * discussion_r3920002298). Anything else is real and stays loud. */
function _consumeInvalidation(reportError: (message: string) => void, context: string) {
	return (error: unknown): void => {
		if (isScopedCommandInvalidated(error)) return;
		reportError(`${context} - ${_message(error)}`);
	};
}

export interface BeatgridResyncGuards {
	/** PARITY-10: fires after load() released [deck]; reclaims it (or wider, see resyncSettlementNeedsFullBarrier) and publishes inside that reclaimed scope - retry/pending/abandon logic lives in beatgrid-resync.ts. isStale is re-asked here, not just inside publish(), so a stale settlement skips reconciliation entirely rather than acting on a replacement track. Full widen/[deck]-occupancy rationale: performance-ipc.svelte.ts's installScopedSyncRunner (r3913492572 / r3913693383 / r3914267990).
	 *
	 * isStale is re-checked a SECOND time inside `run`, not just once before it is
	 * handed to widen(): widen()'s own claim can queue behind other in-flight wide
	 * work (a genuinely open-ended wait, unlike the microtask-scale awaits inside
	 * reconcileAfterBeatgridSettled itself), so dispose() or a fresh load() on
	 * this deck can land in that queueing gap between the outer check and the
	 * moment this task actually starts (discussion_r3918346693). Re-asking right
	 * before the mutating call fires closes that dominant window without
	 * threading a cancellation token through beatgrid-resync.ts's own internals.
	 *
	 * The ports handed to reconcileAfterBeatgridSettled are ALSO wrapped so that
	 * every write it makes - abandon/enable a follower, or retry via
	 * synchronizeFollowers - re-checks isStale() immediately before it lands, not
	 * only at the two checkpoints above. dispose() bumps every deck's loadToken
	 * synchronously and isStale's captured rt reference is orphaned by dispose()
	 * replacing the deck runtime, so once dispose() has run isStale() reads true
	 * for the rest of this continuation's life, including across a subsequent
	 * remount - this wrapper is what makes that permanently-true signal actually
	 * gate the writes instead of only gating whether reconciliation starts at all.
	 *
	 * The guard covers every mutating BeatgridResyncPorts method
	 * reconcileAfterBeatgridSettled can reach, not only the ones its own body
	 * calls directly: takePending/markSettledGridless/markPending are called
	 * from beatgrid-resync.ts's internals it delegates into
	 * (discussion_r3919293432 P1 BLOCKING - an earlier pass guarded only the
	 * three methods visible at this call site and left these three, reachable
	 * one level deeper, free to drain or recreate a freshly remounted deck's
	 * resync tracking). */
	afterBeatgridUpgrade(
		deck: DeckId,
		landed: boolean,
		publish: () => void,
		isStale: () => boolean,
		/** True only when the caller already holds a scheduler claim covering
		 * `deck` plus the wide barrier (the rbx-vs-own switch's
		 * `[...DECK_IDS, 'sync']` claim is the one caller). Skips `runScoped`
		 * entirely and reconciles inline: re-claiming here would await a nested
		 * `[deck]` claim whose only predecessor is the caller's own still-open
		 * claim, a direct self-deadlock (discussion_r3972154599 P1 BLOCKING),
		 * and the reclaim is redundant anyway since the wide scope this
		 * settlement could ever need is already held. */
		alreadyScoped?: boolean
	): Promise<void>;
	/** Fired from `_clearLoadedTrackState`/`unload()` right after `stranded` (the
	 * followers `deck` left pending) is captured, but the actual reconciliation
	 * runs deferred behind a scoped-command claim - `deck`'s own outer claim must
	 * finish first, and widening can then queue behind other in-flight wide work.
	 * A stranded follower can itself be paused/reloaded with a brand new track in
	 * that gap; reconcileBeforeClear's abandon/retry paths write onto the bare
	 * follower id it was given with no way to know that happened (P1 BLOCKING,
	 * discussion_r3914921228). Guard every port write reconcileBeforeClear can
	 * make against each follower's OWN load identity, captured here at the same
	 * instant as `stranded` itself, so a follower whose identity has since
	 * changed is silently excluded rather than incorrectly disabled or re-marked
	 * pending against a master its replacement track never asked to follow.
	 *
	 * That identity is the runtime OBJECT paired with the loadToken, not the
	 * bare numeric token: dispose() installs a fresh runtime per deck whose
	 * loadToken restarts at 0, so a captured number compares equal again after
	 * the same number of loads in a remounted session, and this reconciliation
	 * would then disable or re-mark a deck belonging to a route it never saw
	 * (discussion_r3919692507 P1 BLOCKING). An orphaned runtime reference can
	 * never be `===` the live one again, which is what makes the pair
	 * non-reusable. */
	beforeClear(deck: DeckId, stranded: readonly DeckId[], action: 'reload' | 'unload'): void;
	/** Adopt an authoritative /anlz beatgrid the shared display cache just
	 * learned for `stableId` onto every loaded deck still holding a different
	 * one.
	 *
	 * PARITY-10 merges the synthetic analysis grid into a local track's
	 * `deck.anlz` and `upgradeDeckBeatgrid` then refuses to run again, because
	 * the deck now HAS a grid. When a vendor mapping appears afterwards, the
	 * authoritative PQTZ grid arrives only through the waveform cache's own
	 * ambient `/anlz` retry - which used to reach `WaveRow` alone, leaving
	 * `_quantizeGrid`, beat loops, `_synchronizeFollowers` and the master-grid
	 * read on the stale fallback while the row painted PQTZ
	 * (discussion_r3919779323 P1 BLOCKING).
	 *
	 * It routes through the SAME settlement path a deferred upgrade uses -
	 * publish inside the reclaimed scope, then reconcile - rather than
	 * assigning `deck.anlz` directly, so followers pending on this deck are
	 * re-locked (or abandoned) under the identical widen rules instead of
	 * observing a grid change nothing reconciled. `landed` defaults to true: a
	 * real grid is exactly what a landed settlement means, and the ambient
	 * retry that drives this can only ever discover one. The PARITY-02
	 * rbx-vs-own switch passes it explicitly, because selecting a source that
	 * has no grid for this track REMOVES one, and a removal has to settle
	 * gridless rather than leave a playing follower phase-locked to a grid the
	 * deck no longer has (discussion_r3968213995).
	 *
	 * RETURNS A PROMISE THAT SETTLES ONCE EVERY TRIGGERED RECONCILIATION HAS
	 * (discussion_r3970967293 P1 BLOCKING). Each `afterBeatgridUpgrade` call
	 * used to be fire-and-forget here, so a caller inside the performance
	 * command scheduler's all-deck-plus-sync claim - the rbx-vs-own source
	 * switch is the only one - could report the switch complete, release the
	 * claim, and let a LATER command's own claim be granted while this deck's
	 * audible rescheduling was still queued behind `runScoped`'s widen. The
	 * promise never rejects: every per-deck failure is already funneled
	 * through `_consumeInvalidation`/`reportError` exactly as before, so
	 * awaiting this only postpones "done", it does not turn a swallowed error
	 * into an unhandled rejection. */
	adoptAuthoritativeGrid(
		stableId: string,
		data: AnlzData,
		landed?: boolean,
		/** Forwarded to `afterBeatgridUpgrade` for every matching deck; see its
		 * doc for why this must be true from inside an already-held wide claim. */
		alreadyScoped?: boolean
	): Promise<void>;
	/** Install the concrete scoped runner both wrappers claim through. The
	 * runner itself lives here because these two wrappers are its only
	 * consumers; audio-engine.svelte.ts re-exports this for the route to call.
	 * Full [deck]-then-widen rationale: performance-ipc.svelte.ts's own
	 * installScopedSyncRunner. */
	installScopedSyncRunner: ScopedSyncRunner<DeckId>['install'];
}

export function createBeatgridResyncGuards(deps: BeatgridResyncGuardDeps): BeatgridResyncGuards {
	const { ports, deckRuntime, deckLoadToken, reportError } = deps;
	const { deckStableId, deckAnlz, publishDeckAnlz } = deps;
	const scopedSync = createScopedSyncRunner<DeckId>();
	const runScoped = scopedSync.run;
	const afterBeatgridUpgrade: BeatgridResyncGuards['afterBeatgridUpgrade'] = (
		deck,
		landed,
		publish,
		isStale,
		alreadyScoped = false
	) =>
		_loadResyncModule().then(({ reconcileAfterBeatgridSettled, resyncSettlementNeedsFullBarrier }) => {
			const guardedPorts: BeatgridResyncPorts = {
				...ports,
				setBeatSyncEnabled: (d, enabled) => {
					if (!isStale()) ports.setBeatSyncEnabled(d, enabled);
				},
				setSyncError: (d, message) => {
					if (!isStale()) ports.setSyncError(d, message);
				},
				synchronizeFollowers: (m, f) => (isStale() ? Promise.resolve() : ports.synchronizeFollowers(m, f)),
				markPending: (follower, master) => {
					if (!isStale()) ports.markPending(follower, master);
				},
				takePending: (master) => (isStale() ? [] : ports.takePending(master)),
				markSettledGridless: (d) => {
					if (!isStale()) ports.markSettledGridless(d);
				}
			};
			const run = () => {
				if (isStale()) return Promise.resolve();
				return (publish(), reconcileAfterBeatgridSettled(guardedPorts, deck, landed));
			};
			// Caller already holds [deck] and the wide barrier (both are inside
			// its own [...DECK_IDS, 'sync'] claim), so run inline: a fresh
			// runScoped(deck, ...) here would await a nested claim whose only
			// predecessor is that same still-open outer claim, a direct
			// self-deadlock (discussion_r3972154599 P1 BLOCKING).
			if (alreadyScoped) return run();
			return runScoped(deck, (widen) => {
				if (isStale()) return Promise.resolve();
				if (!resyncSettlementNeedsFullBarrier(guardedPorts, deck, landed)) return run();
				return (
					widen(run).catch((error: unknown) =>
						reportError(`Deck ${deck}'s Beat Sync reconciliation across decks failed - ${_message(error)}`)
					),
					Promise.resolve()
				);
			});
		});
	return {
		installScopedSyncRunner: scopedSync.install,
		afterBeatgridUpgrade,
		beforeClear(deck, stranded, action) {
			if (stranded.length === 0) return;
			// `deck` itself needs the same non-reusable identity as its
			// followers: a PLAY for a REPLACEMENT track can elect it master
			// under this same deck id before the widened reconciliation below
			// actually runs. Reading that as "master === deck, already
			// reconciled" would silently discard the followers `deck`'s OLD
			// track left stranded - they were never waiting on the
			// replacement, and nothing else will ever abandon or retry them
			// once this pre-clear path skips (discussion_r3920394933).
			const deckIdentityAtCapture = {
				runtime: deckRuntime(deck),
				token: deckLoadToken(deck),
				stableId: deckStableId(deck)
			};
			const deckIsCurrent = (): boolean =>
				deckRuntime(deck) === deckIdentityAtCapture.runtime &&
				deckLoadToken(deck) === deckIdentityAtCapture.token &&
				deckStableId(deck) === deckIdentityAtCapture.stableId;
			const strandedIdentities = new Map(
				stranded.map((follower): [DeckId, { runtime: object; token: number }] => [
					follower,
					{ runtime: deckRuntime(follower), token: deckLoadToken(follower) }
				])
			);
			const followerIsCurrent = (follower: DeckId): boolean => {
				const captured = strandedIdentities.get(follower);
				if (captured === undefined) return false;
				return deckRuntime(follower) === captured.runtime && deckLoadToken(follower) === captured.token;
			};
			const guardedPorts: BeatgridResyncPorts = {
				...ports,
				syncMaster: () => {
					const master = ports.syncMaster();
					return master === deck && !deckIsCurrent() ? null : master;
				},
				setBeatSyncEnabled: (follower, enabled) => {
					if (followerIsCurrent(follower)) ports.setBeatSyncEnabled(follower, enabled);
				},
				setSyncError: (follower, message) => {
					if (followerIsCurrent(follower)) ports.setSyncError(follower, message);
				},
				markPending: (follower, master) => {
					if (followerIsCurrent(follower)) ports.markPending(follower, master);
				},
				synchronizeFollowers: (master, followers) =>
					ports.synchronizeFollowers(master, followers.filter(followerIsCurrent))
			};
			const onFailure = (error: unknown) =>
				reportError(
					`Deck ${deck}'s stranded Beat Sync followers could not be reconciled before ${action} - ${_message(error)}`
				);
			runScoped(deck, (widen) =>
				_loadResyncModule().then(({ reconcileBeforeClear }) =>
					widen(() => reconcileBeforeClear(guardedPorts, deck, stranded)).catch(onFailure)
				)
			).catch(onFailure);
		},
		adoptAuthoritativeGrid(stableId, data, landed = true, alreadyScoped = false) {
			const settlements: Promise<void>[] = [];
			for (const deck of ports.deckIds) {
				if (deckStableId(deck) !== stableId) continue;
				const current = deckAnlz(deck);
				if (current === null || sameBeatgrid(current.beatgrid, data.beatgrid)) continue;
				const runtime = deckRuntime(deck);
				const token = deckLoadToken(deck);
				const isStale = (): boolean =>
					deckRuntime(deck) !== runtime ||
					deckLoadToken(deck) !== token ||
					deckStableId(deck) !== stableId;
				const next: AnlzData = { ...current, beatgrid: data.beatgrid };
				settlements.push(
					afterBeatgridUpgrade(
						deck,
						landed,
						() => {
							// Re-read rather than trust `current`: the publish thunk runs
							// inside a scope claim this call had to queue for, and a
							// reload can have replaced the payload in that gap. The grid
							// is what this adoption is about, so it is merged onto
							// whatever payload is live at publish time, not the one read
							// when the cache fired.
							const latest = deckAnlz(deck);
							publishDeckAnlz(deck, latest === null ? next : { ...latest, beatgrid: data.beatgrid });
						},
						isStale,
						alreadyScoped
					).catch(
						_consumeInvalidation(
							reportError,
							`Deck ${deck}'s beatgrid could not be updated to the analysis grid rekordbox now has`
						)
					)
				);
			}
			return Promise.all(settlements).then(() => undefined);
		}
	};
}
