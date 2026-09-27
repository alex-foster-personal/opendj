/**
 * PARITY-02: rbx-vs-own analysis source toggle -- reactive client for
 * GET/PUT /api/v1/analysis/source.
 *
 * The backend (apps.analysis.selection) models this as a per-lane PERSISTED
 * default plus a per-lane IN-MEMORY dev toggle, with `effective = toggle
 * unless toggle is unset, else default`. This UI is a binary rbx/own
 * switch and is documented (spec section 3, PARITY-02) as a TESTING/DEV
 * affordance that never survives a relaunch -- so it writes ONLY the
 * toggle half, never the persisted default, and reads the resolved
 * `effective` value. That also means this UI cannot return a lane to
 * `unset`: the daemon does that itself on every launch (reset_toggles()),
 * and there is no third button here to ask for it early.
 *
 * Deliberately NOT localStorage-backed, unlike prefs.svelte.ts: the daemon
 * is the single source of truth; this module just mirrors it into a $state
 * rune so the TopBar dropdown and any other reader stay in sync with what
 * the server actually has selected, including a change an agent made
 * directly over the endpoint.
 *
 * .svelte.ts extension is REQUIRED for the $state rune (RECON-FRONTEND 10.1).
 */
import { ApiError, api, readApiErrorCode, readApiErrorStatus, unwrap } from '../api';
import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
import {
	evictAnlzCacheEntriesServingOtherSource,
	invalidateAllAnlzCacheEntries,
	refreshAnalysisSourceDecks
} from '$lib/components/rb/wave/anlz-cache.svelte';
import { bumpAnlzFetchGeneration } from '$lib/rb/anlz-fetch-generation';
import {
	ANALYSIS_SOURCE_FEATURES,
	analysisSourceState,
	type AnalysisSource,
	type AnalysisSourceFeature
} from '$lib/rb/analysis-source-state.svelte';
import { DECK_IDS, deckStates } from './audio-engine.svelte';

// Re-exported for every existing consumer of this module: the state itself
// lives in analysis-source-state.svelte.ts (no audio-engine.svelte.ts
// dependency) so beatgrid-upgrade.ts can read it without importing this
// module and closing an import cycle back through audio-engine.svelte.ts.
export { ANALYSIS_SOURCE_FEATURES, analysisSourceState };
export type { AnalysisSource, AnalysisSourceFeature };

// This UI's feature id doubles as the backend's lane name (both "beatgrid"),
// but the two vocabularies are declared independently on purpose: the wire
// vocabulary here is UI-facing and predates apps.analysis.lanes.
const _LANE_OF_FEATURE = Object.fromEntries(ANALYSIS_SOURCE_FEATURES.map((feature) => [feature, feature])) as Record<AnalysisSourceFeature, string>;

const _UI_SOURCE_OF_EFFECTIVE: Record<string, AnalysisSource> = { rbx: 'rekordbox', own: 'own' };
const _TOGGLE_OF_UI_SOURCE: Record<AnalysisSource, 'rbx' | 'own'> = { rekordbox: 'rbx', own: 'own' };

// A poll may begin before a local PUT and settle after it. The daemon then
// correctly answers the PUT with the new source, but the older GET must never
// replace that confirmed mirror after its refresh. Increment before issuing a
// mutation so every earlier GET response is known stale at adoption time.
let _latestMutation = 0;

// `_latestMutation` orders a GET against a local PUT; it does NOT order two
// GETs against each other, because a poll never touches it. Two overlapping
// polls therefore both carried the same mutation value, so a slow one that
// overtook a fast one could adopt an OLDER daemon answer last: the mirror
// showed RBX while the daemon and the decks were on OWN, and a click on that
// stale RBX highlight then read as "no change" to the change check and left
// the decks where they were for good (discussion_r3968534418 P1 BLOCKING).
// Stamp every GET and never adopt one older than the newest already adopted.
let _latestPollIssued = 0;
let _latestPollAdopted = 0;

/** The source the DECKS are actually on, which is not always what the daemon
 * last told us and is therefore not `analysisSourceState.features`.
 *
 * `_refreshDecks` is an unbounded await -- it queues behind every in-flight
 * performance command before it fetches anything -- and `/anlz` resolves
 * rbx-vs-own SERVER-side at fetch time. A selection change DURING that wait
 * therefore hands the decks the new source while this call is still holding
 * the older GET's answer, and neither sequence guard above can see it: both
 * order local PUTs and polls against each other, not against the daemon
 * (discussion_r3970117741 P1 BLOCKING). Comparing the next answer against the
 * MIRROR then reads "no change" and leaves the decks on the opposite grid for
 * good, and clicking the stale highlight is a no-op for the same reason.
 * Comparing against what the decks HOLD cannot lie: it is read back off the
 * payloads the server actually served (`beatgrid_source`). Lives on the shared
 * rune (`analysisSourceState.deckFeatures`) rather than in a module-private
 * map so it resets with the rest of the state and is readable by anyone
 * diagnosing a split. */

/** A record-change refresh that failed and has nobody else to retry it. This
 * path never writes `analysisSourceState.features`, so the next poll compares
 * own against own, sees no change, and would never come back for it -- while
 * the failed refresh has ALREADY bumped the fetch generation and emptied the
 * shared ANLZ cache, leaving the deck on its terminal pre-change payload
 * (discussion_r3969942725 P1 BLOCKING). Held here and drained by the existing
 * 5s poll rather than by a second timer of its own. */
let _recordRefreshPending = false;

/** Deck refreshes in flight. A faster no-refresh switch that wins while one is
 * still awaiting /anlz must bump the fetch generation so the slower batch's
 * pre-publication guard can discard itself even when the mirror never moved
 * (discussion_r3975326238 P1 BLOCKING). */
let _sourceRefreshInFlight = 0;

/** Bumped every time a record-change event marks a refresh pending. Lets
 * `_refreshDecks` tell "a refresh that started before this change" apart
 * from "a refresh that can actually have seen it" (discussion_r3972154604
 * P2 BLOCKING). */
let _recordRefreshGeneration = 0;

/** True when `next` disagrees with the source the decks are actually holding.
 * A feature seen for the FIRST time (a fresh mount's `{}` -> populated) is not
 * a change WHEN NOTHING IS LOADED: nothing was fetched under the old value
 * yet, so there is nothing stale to refresh -- this keeps the poll in
 * AnalysisSourceToggle.svelte from forcing a multi-MB /anlz refetch on every
 * tick when nothing moved.
 *
 * But a deck CAN already be loaded the first time this ever runs (a track
 * finishes loading before the initial GET settles, or before an operator's
 * very first switch), and its /anlz was fetched under whatever source was
 * effective at that moment - which this unknown watermark cannot vouch for
 * either way. Treating "unknown" as "no disagreement" there lets `_adopt`
 * record the requested source as the watermark without ever refreshing the
 * deck, so a subsequent poll compares the new answer against that
 * rubber-stamped watermark, sees no change, and leaves the deck on its
 * pre-switch grid indefinitely while every readout reports the new source
 * (discussion_r3973129047 P1 BLOCKING). So an unknown watermark forces a
 * refresh whenever ANY deck actually holds a track, whatever `next` says -
 * the refresh is what lets `_recordDeckSources` learn the true watermark from
 * what /anlz actually serves, instead of guessing it matches.
 *
 * Reads `analysisSourceState.deckFeatures`, NOT `.features`. The mirror is a
 * snapshot of what the daemon said; only `deckFeatures` is a statement about
 * what would actually be stale. */
function _decksDisagreeWith(next: Record<string, AnalysisSource>): boolean {
	for (const feature of ANALYSIS_SOURCE_FEATURES) {
		const source = next[feature];
		if (source === undefined) continue;
		const held = analysisSourceState.deckFeatures[feature];
		if (held !== undefined) {
			if (held !== source) return true;
			continue;
		}
		// Only beatgrid is deck-stamped on `/anlz` today; other lanes are toggle-only.
		if (feature !== 'beatgrid') continue;
		if (DECK_IDS.some((deck) => deckStates[deck].stable_id !== null)) return true;
	}
	return false;
}

/** Records what the decks now hold. `served` is the source `/anlz` stamped on
 * the payloads it actually returned; `null` means no deck had a track loaded,
 * so nothing was served and the intended selection is the honest watermark. */
function _recordDeckSources(
	intended: Record<string, AnalysisSource>,
	served: AnalysisSource | null
): void {
	const beatgridSource = intended.beatgrid;
	if (beatgridSource === undefined) return;
	analysisSourceState.deckFeatures.beatgrid =
		served !== null ? served : beatgridSource;
}

/** Adopts a confirmed selection from the daemon (a GET mirror or a PUT
 * response) and, only when a feature actually changed value, forces the
 * loaded decks and shared ANLZ cache off the pre-switch payload -- see
 * `refreshAnalysisSourceDecks` (anlz-cache.svelte.ts) for why
 * that is not automatic: the same /anlz URL now returns different bytes, and
 * neither the deck's terminal state nor the one-fetch-per-session cache nor
 * the response's own 1h HTTP cache header would otherwise notice
 * (discussion_r3921666943). Runs for a poll-detected external PUT exactly
 * like a local one -- an agent driving the endpoint directly must get the
 * same live effect as clicking the UI control.
 *
 * NEITHER the mirror NOR the deck watermark is written before the refresh
 * settles: if a loaded deck's cache-bypassing /anlz refresh rejects (a
 * transient network error, an ANALYSIS_NOT_FOUND from the new source), the
 * watermark must stay on the OLD value so `_decksDisagreeWith` still sees a
 * disagreement against the next poll's answer and retries the refresh --
 * advancing it before the refresh is confirmed would make that poll compare
 * next-against-next, read no change, and leave the failed deck's stale
 * pre-switch anlz in place indefinitely while the toggle reports the new
 * source (discussion_r3921666943 follow-up). Throws through to the caller
 * (setAnalysisSource/loadAnalysisSource) on failure -- no silent partial
 * adoption, and `setAnalysisSource` puts the daemon back (`_rollBackFailedSwitch`)
 * rather than leaving it split from the decks.
 *
 * `isSuperseded` is asked again AFTER the refresh, not only before it: the
 * refresh is an unbounded await, and whichever response is newest at the end
 * of it owns the mirror. The two callers define "newer" differently (a local
 * PUT vs. a later poll), which is why this takes a predicate rather than a
 * bare mutation number. */
async function _adopt(
	features: Record<string, AnalysisSource>,
	isSuperseded: () => boolean,
	serialize: boolean
): Promise<void> {
	if (isSuperseded()) return;
	if (!_decksDisagreeWith(features)) {
		// No LOADED deck disagrees, but a track merely prefetched (library
		// browsing's ensureAnlz, never loaded onto a deck) is invisible to that
		// check and would otherwise keep serving pre-switch bytes to whichever
		// deck loads it next (discussion_r3973991969 P1 BLOCKING). Bump the
		// generation only when something was actually evicted: an unrelated
		// switch (or a re-adoption of the same source) must not force every
		// in-flight fetch elsewhere to re-check itself for nothing.
		const beatgrid = features.beatgrid;
		if (beatgrid !== undefined) {
			if (_sourceRefreshInFlight > 0) {
				bumpAnlzFetchGeneration();
			}
			// `evictAnlzCacheEntriesServingOtherSource` only clears READY entries
			// - a LOADING one (a prefetch already in flight when this adoption
			// runs) is invisible to it, since we do not yet know what source it
			// will resolve to (discussion_r3975043552 P1 BLOCKING). A generation
			// bump is what actually protects that case: `_fetchAndPublish`
			// discards any settle whose captured generation went stale, so
			// bumping forces even an untouched in-flight fetch to re-validate
			// itself against the source it settles under. Fire it whenever the
			// effective value is actually changing, not only when the eviction
			// found a ready entry to remove.
			const evictedReady = evictAnlzCacheEntriesServingOtherSource(beatgrid);
			const valueChanged = analysisSourceState.features.beatgrid !== beatgrid;
			if (evictedReady || valueChanged) {
				bumpAnlzFetchGeneration();
			}
		}
		_recordDeckSources(features, null);
		if (isSuperseded()) return;
		analysisSourceState.features = features;
		return;
	}
	const served = await _refreshDecks(serialize, isSuperseded);
	if (isSuperseded()) return;
	// The watermark is what the SERVER stamped on the payloads it just served,
	// not `features`. If the daemon moved during the refresh those two differ,
	// and recording `features` here would re-create exactly the stuck state
	// this watermark exists to prevent (discussion_r3970117741).
	_recordDeckSources(features, served);
	analysisSourceState.features = features;
}

/** Pulls the daemon's current per-feature selection. Call on mount and on a
 * poll: the server is authoritative and an agent may have changed it since
 * (AnalysisSourceToggle.svelte's poll is what makes the latter visible).
 *
 * Serializes its deck refresh through the performance command scheduler
 * (`serialize: true`), unlike setAnalysisSource below: this path is NOT
 * already inside a command claim, so without it a poll that notices an
 * agent's direct PUT would invalidate and replace every deck's grid in the
 * middle of a PREPARE/START or another deck mutation
 * (discussion_r3968214009 P1 BLOCKING). */
export async function loadAnalysisSource(): Promise<void> {
	const mutation = _latestMutation;
	const poll = ++_latestPollIssued;
	const body = await unwrap(api.GET('/api/v1/analysis/source'));
	const isSuperseded = (): boolean => mutation !== _latestMutation || poll < _latestPollAdopted;
	if (isSuperseded()) {
		await _drainPendingRecordRefresh();
		return;
	}
	_latestPollAdopted = poll;
	const features = _featuresOf(body);
	await _adopt(features, isSuperseded, true);
	await _drainPendingRecordRefresh();
}

/** The UI feature map from one wire body. */
function _featuresOf(body: { lanes: Record<string, { effective: string; toggle: string }>; serving?: readonly string[] }): Record<string, AnalysisSource> {
	const features: Record<string, AnalysisSource> = {};
	for (const feature of ANALYSIS_SOURCE_FEATURES) {
		const lane = body.lanes[_LANE_OF_FEATURE[feature]];
		if (lane === undefined) continue;
		features[feature] = _UI_SOURCE_OF_EFFECTIVE[lane.effective];
	}
	analysisSourceState.serving = body.serving ?? [];
	return features;
}

/** Sets one feature's source. Writes the in-memory dev TOGGLE, never the
 * persisted default -- this control is documented to reset on relaunch, and
 * the persisted half is a separate, promotion-only concept the UI here does
 * not expose. Fails loudly (throws) on a rejected feature name or a daemon
 * error -- no optimistic local write, since a silently unapplied switch is
 * exactly the defect PARITY-02 exists to prevent. */
export async function setAnalysisSource(
	feature: AnalysisSourceFeature,
	source: AnalysisSource
): Promise<void> {
	const lane = _LANE_OF_FEATURE[feature];
	const attemptedToggle = _TOGGLE_OF_UI_SOURCE[source];
	const mutation = ++_latestMutation;
	// A local PUT must retire any poll GET already in flight: mutation alone
	// orders against older PUTs, but a GET that started before this PUT shares
	// the same captured mutation until it returns and must not adopt after this
	// write confirms (analysis-source.test.mjs "GET begun before a local PUT").
	_latestPollAdopted = ++_latestPollIssued;
	const body = await unwrap(
		api.PUT('/api/v1/analysis/source', {
			body: { lane, toggle: attemptedToggle }
		})
	);
	// The server's own account of what this PUT just displaced, read and
	// overwritten under the same lock acquisition as the write. A client-side
	// cache of an earlier GET/PUT answer can be stale by the time THIS PUT
	// lands if a concurrent agent's own PUT landed in between, so a rollback
	// keyed off that cache would restore the wrong prior value
	// (discussion_r3974235454 P1 BLOCKING).
	const displacedToggle = body.previous_toggle ?? undefined;
	// The revision THIS write produced, so the rollback's compare-and-set can
	// require that nothing has touched the toggle since - a value-only
	// `expected_toggle` still round-trips through own -> rbx -> own back to
	// `attemptedToggle` and would let a stale rollback clobber a newer write
	// that merely landed on the same value (discussion_r3976638752 P2
	// BLOCKING).
	const attemptedToggleRevision = body.lanes[lane]?.toggle_revision;
	const features = _featuresOf(body);
	try {
		// `serialize: false`: the ONLY caller is the `analysis_source` performance
		// command (performance-ipc.svelte.ts), which already holds a claim on
		// [...DECK_IDS, 'sync'] for the whole of this call. Claiming those scopes a
		// second time from inside that claim would wait on its own tail forever.
		await _adopt(features, () => mutation !== _latestMutation, false);
	} catch (exc) {
		await _rollBackFailedSwitch(
			lane,
			displacedToggle,
			attemptedToggle,
			attemptedToggleRevision,
			mutation
		);
		throw exc;
	}
}

/** Puts the daemon back where the failed switch found it.
 *
 * The PUT commits the daemon's toggle BEFORE `_adopt` can reject, so a switch
 * whose deck refresh fails permanently (a corrupt own record answering /anlz
 * with a 500, say) otherwise leaves the daemon on the new source while the
 * decks and the mirror stay on the old one, and every poll re-runs the same
 * failing refresh forever (discussion_r3970117737 P1 BLOCKING). Two resources
 * with no shared transaction cannot be made atomic from here, so this is a
 * compensating action, not a rollback: the switch ends where it started, and
 * the caller still gets the original error rather than a silent no-op.
 *
 * Restores the TOGGLE verbatim, including `unset`. Writing back the mirror's
 * `effective` instead would pin a toggle that was previously unset, which is a
 * different daemon state that this UI has no button to undo (see the module
 * header). A newer local mutation means someone has since asked for something
 * else and owns the daemon now, so this stands down rather than clobbering it.
 *
 * `mutation` only orders this against a NEWER local PUT from THIS module -
 * the endpoint is explicitly agent-facing, and an agent driving it directly
 * over HTTP never touches `_latestMutation` at all. So this compensating PUT
 * also carries `expected_toggle: attemptedToggle` (the value this failed
 * switch itself set, captured from its own PUT response before the failure),
 * which the SERVER applies as an atomic compare-and-set
 * (`apps.analysis.selection.compare_and_set_toggle`): it sets the toggle only
 * if the lane's CURRENT value still equals `attemptedToggle`, checked and
 * written under one lock acquisition. A separate client-side GET-then-PUT
 * leaves the exact window this exists to close open on the CLIENT side - an
 * agent's own PUT can land between this module's GET and its compensating PUT
 * and still get overwritten, because nothing atomic ties the two together
 * (discussion_r3973129053 P2 BLOCKING, sharpening discussion_r3972682728). If
 * the daemon no longer holds what this switch put there, somebody else's
 * change is now live and owns the daemon; the server refuses with 409 rather
 * than clobbering that newer change with a stale one nobody asked for.
 *
 * `attemptedToggleRevision` closes an ABA gap `expected_toggle` alone cannot:
 * a VALUE-only compare-and-set still succeeds after an external round trip
 * `attemptedToggle -> other -> attemptedToggle`, because the current value
 * equals `attemptedToggle` again even though a newer write landed in between.
 * Passed through as `expected_toggle_revision`, which the server additionally
 * requires to still equal the revision THIS switch's own PUT produced
 * (discussion_r3976638752 P2 BLOCKING). `undefined` (a lane the server has
 * never reported a revision for) degrades to the value-only check exactly as
 * before this field existed. */
async function _rollBackFailedSwitch(
	lane: string,
	displacedToggle: string | undefined,
	attemptedToggle: string,
	attemptedToggleRevision: number | undefined,
	mutation: number
): Promise<void> {
	if (mutation !== _latestMutation) return;
	if (displacedToggle === undefined) {
		console.error(
			`[analysis-source] switch of ${lane} failed with no observed prior toggle to ` +
				'restore, so the daemon keeps the new source; reload to resync'
		);
		return;
	}
	_latestMutation++;
	_latestPollAdopted = ++_latestPollIssued;
	// The failed switch's own refresh already bumped the fetch generation and
	// may have wiped or partially repopulated the shared ANLZ cache before it
	// failed. A retry timer or an unrelated prefetch (`ensureAnlz`) that
	// issued its own /anlz request under that same generation can still be in
	// flight right now, and the server may still answer it with the FAILED
	// switch's source until this compensating PUT actually lands. Bumping
	// again here - before that PUT, so it covers every outcome below,
	// including the 409 stand-down and the swallowed-error path - forces any
	// such straggler to discard itself at settle
	// (`_fetchAndPublish`'s existing generation-mismatch check) instead of
	// publishing onto a deck after the daemon is already back on the old
	// source, which neither `features` nor `deckFeatures` would ever detect
	// as a disagreement (discussion_r3975043558 P1 BLOCKING).
	bumpAnlzFetchGeneration();
	invalidateAllAnlzCacheEntries();
	try {
		// The compensating PUT's own answer needs no further capture: the value
		// it restores was the server's own `previous_toggle` from the failed
		// switch's PUT, not a client-side cache that would otherwise need
		// re-syncing here (discussion_r3970967286 P1 BLOCKING, discussion_r3974235454).
		const rollbackBody: {
			lane: string;
			toggle: string;
			expected_toggle: string;
			expected_toggle_revision?: number;
		} = {
			lane,
			toggle: displacedToggle,
			expected_toggle: attemptedToggle
		};
		if (attemptedToggleRevision !== undefined) {
			rollbackBody.expected_toggle_revision = attemptedToggleRevision;
		}
		await unwrap(api.PUT('/api/v1/analysis/source', { body: rollbackBody }));
	} catch (exc) {
		if (
			(exc instanceof ApiError && exc.status === 409) ||
			readApiErrorStatus(exc) === 409 ||
			readApiErrorCode(exc) === 'toggle_changed'
		) {
			console.error(
				`[analysis-source] switch of ${lane} failed, but the daemon no longer holds ` +
					`the '${attemptedToggle}' this switch itself set - someone else changed it ` +
					'since, so standing down rather than overwriting a newer change with the ' +
					'stale pre-switch value'
			);
			return;
		}
		// Reported, never swallowed: the daemon is now genuinely split from the
		// decks and the operator has to know that reloading is the way back.
		console.error(
			`[analysis-source] could not restore ${lane} to '${displacedToggle}' after a ` +
				'failed switch; the daemon is on the new source and the decks are not',
			exc
		);
	}
}

// ------------------------------------------------------- serialized refresh

/** Runs `work` under the performance command scheduler's all-deck-plus-sync
 * scopes. Injected rather than imported: performance-ipc.svelte.ts owns the
 * one scheduler and already imports setAnalysisSource from here, so importing
 * it back would close a module cycle. Same install-a-port shape
 * `installAuthoritativeAnlzGridSink` and `installScopedSyncRunner` use. */
export type AnalysisSourceRefreshRunner = <T>(work: () => Promise<T>) => Promise<T>;

let _refreshRunner: AnalysisSourceRefreshRunner | null = null;

export function installAnalysisSourceRefreshRunner(runner: AnalysisSourceRefreshRunner): void {
	if (_refreshRunner !== null) {
		throw new Error('an analysis source refresh runner is already installed');
	}
	_refreshRunner = runner;
}

/** Throws rather than falling back to an unserialized refresh: a missing
 * runner is a wiring bug, and silently running the deck swap outside the
 * command queue is exactly the defect the runner exists to close.
 *
 * `isSuperseded` defaults to "never" for `_drainPendingRecordRefresh`'s
 * record-change call site, which has no source-switch race to guard against
 * (it owns `_recordRefreshGeneration` for its own ordering). `_adopt` passes
 * its real predicate through so `refreshAnalysisSourceDecks` can decline to
 * publish a fetch that a newer switch has already overtaken
 * (discussion_r3975326238 P1 BLOCKING) - checking only AFTER this function
 * returns, as `_adopt` already did, is too late: the decks and shared cache
 * have been written by then. */
async function _refreshDecks(
	serialize: boolean,
	isSuperseded: () => boolean = () => false
): Promise<AnalysisSource | null> {
	// Captured before the unbounded awaits below: a record-change event that
	// arrives WHILE this refresh is in flight bumps the generation past this,
	// so completing must not clear a pending mark it cannot have satisfied
	// (discussion_r3972154604 P2 BLOCKING).
	const requestedGeneration = _recordRefreshGeneration;
	let served: AnalysisSource | null;
	_sourceRefreshInFlight += 1;
	try {
		if (serialize) {
			if (_refreshRunner === null) {
				throw new Error('no analysis source refresh runner is installed');
			}
			served = await _refreshRunner(() =>
				refreshAnalysisSourceDecks(DECK_IDS, deckStates, isSuperseded)
			);
			if (served === undefined) {
				// A runner that awaits the work but drops its result would silently
				// write `undefined` into the deck watermark, and every later
				// comparison against it would read "never refreshed". Loud, because
				// the runner is injected and TypeScript cannot enforce this at the
				// installation site's runtime.
				throw new Error('the analysis source refresh runner dropped its work result');
			}
		} else {
			served = await refreshAnalysisSourceDecks(DECK_IDS, deckStates, isSuperseded);
		}
	} finally {
		_sourceRefreshInFlight -= 1;
	}
	// Any successful refresh refetches EVERY loaded deck, so it satisfies a
	// pending record-change retry whatever triggered it - unless a NEWER
	// change arrived after this refresh's fetches were already dispatched,
	// in which case it is that later refresh's job to clear the flag.
	if (_recordRefreshGeneration === requestedGeneration) _recordRefreshPending = false;
	return served;
}

/** True while any exposed feature reads its lane from our own analysis rather
 * than from rekordbox. */
function _anyFeatureIsOwn(): boolean {
	return ANALYSIS_SOURCE_FEATURES.some((feature) => analysisSourceState.features[feature] === 'own');
}

/** Re-runs the deck/cache refresh when the library's analysis records change
 * underneath an OWN selection.
 *
 * `/anlz`'s beatgrid is read from the latest `analysis` record whenever the
 * beatgrid lane is on 'own' (rb_assets.py `_resolve_beatgrid_source` ->
 * `_load_latest_record`), so Refresh Analysis replacing that record changes
 * what a fresh `/anlz` would return. Nothing noticed: the shared cache entry
 * is still 'ready', `deckAnlzNeedsFetch` is still false, and the deck keeps
 * painting and beat-matching the superseded experimental grid until someone
 * toggles the source or reloads the page (discussion_r3968534441 P1
 * BLOCKING). On 'rekordbox' the payload does not read that table at all, so
 * this deliberately does nothing - a metadata edit must not cost every loaded
 * deck a multi-MB refetch for a field it cannot have changed.
 *
 * `kind: 'tracks'` is published for a completed ingest refresh
 * (ingest.py `_refresh_worker`, always with an empty `ids`), but the same
 * kind is ALSO published with concrete stable_ids for ordinary metadata
 * edits that never touch the analysis record (`routes/tracks.py`,
 * `bulk_edit.py`, `find_replace.py`); `_onTracksChanged` below filters for
 * the genuine signal so those do not each cost every loaded deck a
 * multi-MB all-deck-plus-sync refetch (discussion_r3977115115 P1 BLOCKING).
 * Returns the unsubscribe for the caller's effect cleanup. */
export function subscribeAnalysisRecordChanges(): () => void {
	const unsubscribeKind = subscribeKind('tracks', _onTracksChanged);
	// The bus fires resync on reconnect, a sequence gap, a malformed frame and a
	// slow-consumer drop, and it replays NOTHING: `_fireResync` walks the resync
	// listeners only, never the kind listeners (events-bus.ts) -- "a consumer
	// that handles a kind MUST also handle resync" (events-bus.ts's own header),
	// so a missed analysis-completion event left a loaded OWN deck on the
	// superseded grid indefinitely (discussion_r3969942717 P1 BLOCKING). A
	// resync carries no ids and means "you may have missed anything", so unlike
	// `_onTracksChanged` it always refreshes unconditionally.
	const unsubscribeResync = subscribeResync(() => _refreshOwnGridsAfterRecordChange());
	return () => {
		unsubscribeKind();
		unsubscribeResync();
	};
}

/** The genuine ingest-completion signal always carries an empty `ids`; an
 * edit naming a currently loaded deck is matched too, defensively, since a
 * loaded deck is exactly what an unnecessary refresh is expensive for.
 * Anything else is an edit to an unloaded track and is dropped. */
function _onTracksChanged(ids: string[]): void {
	const touchesLoadedDeck = DECK_IDS.some((deck) => {
		const stable_id = deckStates[deck].stable_id;
		return stable_id !== null && ids.includes(stable_id);
	});
	if (ids.length !== 0 && !touchesLoadedDeck) return;
	_refreshOwnGridsAfterRecordChange();
}

function _refreshOwnGridsAfterRecordChange(): void {
	if (!_anyFeatureIsOwn()) return;
	_recordRefreshPending = true;
	_recordRefreshGeneration += 1;
	void _drainPendingRecordRefresh();
}

/** The generation-tagged drain currently running, so SAME-generation callers
 * (overlapping polls, or a poll racing the event that just started it) await
 * this one refresh instead of each enqueuing their own, which used to build
 * a backlog behind a slow refresh (discussion_r3977115120 P1 BLOCKING).
 * Tagged by generation, not a bare flag: a change that bumps
 * `_recordRefreshGeneration` mid-drain cannot be reflected by it -
 * `_refreshDecks` already declines to clear `_recordRefreshPending` for a
 * newer generation than the one it started with (discussion_r3972154604) -
 * so that newer generation must still open its own concurrent drain rather
 * than being coalesced into this one. */
let _drainInFlight: Promise<void> | null = null;
let _drainInFlightGeneration: number | null = null;

/** Runs a pending record-change refresh, and LEAVES IT PENDING if it fails so
 * the existing 5s poll retries it (discussion_r3969942725 P1 BLOCKING). */
async function _drainPendingRecordRefresh(): Promise<void> {
	if (!_recordRefreshPending) return;
	if (_drainInFlight !== null && _drainInFlightGeneration === _recordRefreshGeneration) {
		await _drainInFlight;
		if (_recordRefreshPending) {
			return _drainPendingRecordRefresh();
		}
		return;
	}
	const generation = _recordRefreshGeneration;
	_drainInFlightGeneration = generation;
	// `_refreshDecks` never goes through `_adopt`, so `served` can legitimately
	// differ from `features` (an external client flipping the daemon mid-flight);
	// `_recordDeckSources` records what actually got served, not the stale intent.
	_drainInFlight = _refreshDecks(true)
		.then((served) => _recordDeckSources(analysisSourceState.features, served))
		.catch((exc) => {
			console.error(
				'[analysis-source] own-grid refresh after an analysis record change failed; ' +
					'still pending, retrying on the next poll',
				exc
			);
		})
		.finally(() => {
			if (_drainInFlightGeneration === generation) {
				_drainInFlight = null;
				_drainInFlightGeneration = null;
			}
		});
	return _drainInFlight;
}
