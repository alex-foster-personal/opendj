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
import { api, unwrap } from '../api';
import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
import { refreshAnalysisSourceDecks } from '$lib/components/rb/wave/anlz-cache.svelte';
import { DECK_IDS, deckStates } from './audio-engine.svelte';

export type AnalysisSource = 'rekordbox' | 'own';

/** Features with a genuine own-rolled counterpart to A/B against rekordbox.
 * Mirrors the one lane apps.analysis.lanes' 5 lanes that this UI exposes --
 * widen only once another lane grows a real own-rolled counterpart. */
export const ANALYSIS_SOURCE_FEATURES = ['beatgrid'] as const;
export type AnalysisSourceFeature = (typeof ANALYSIS_SOURCE_FEATURES)[number];

// This UI's feature id doubles as the backend's lane name (both "beatgrid"),
// but the two vocabularies are declared independently on purpose: the wire
// vocabulary here is UI-facing and predates apps.analysis.lanes.
const _LANE_OF_FEATURE: Record<AnalysisSourceFeature, string> = { beatgrid: 'beatgrid' };

const _UI_SOURCE_OF_EFFECTIVE: Record<string, AnalysisSource> = { rbx: 'rekordbox', own: 'own' };
const _TOGGLE_OF_UI_SOURCE: Record<AnalysisSource, 'rbx' | 'own'> = { rekordbox: 'rbx', own: 'own' };

interface AnalysisSourceState {
	/** What the DAEMON last told us it has selected. Drives the visible control. */
	features: Record<string, AnalysisSource>;
	/** What the loaded DECKS are actually holding. See `_recordDeckSources`.
	 * Published rather than kept module-private because the two can legitimately
	 * disagree for a poll interval, and an operator or agent asking "why is this
	 * deck on the other grid" needs to read both halves, not infer one. */
	deckFeatures: Record<string, AnalysisSource>;
}

export const analysisSourceState = $state<AnalysisSourceState>({
	features: {},
	deckFeatures: {}
});

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

/** The daemon's per-lane TOGGLE as of the last answer we saw, kept so a failed
 * switch can put back exactly what it displaced (see `_rollBackFailedSwitch`).
 * The UI reads `effective`, which cannot express `unset`, so the toggle is
 * captured verbatim rather than derived from the mirror. */
const _lastToggles: Record<string, string> = {};

/** A record-change refresh that failed and has nobody else to retry it. This
 * path never writes `analysisSourceState.features`, so the next poll compares
 * own against own, sees no change, and would never come back for it -- while
 * the failed refresh has ALREADY bumped the fetch generation and emptied the
 * shared ANLZ cache, leaving the deck on its terminal pre-change payload
 * (discussion_r3969942725 P1 BLOCKING). Held here and drained by the existing
 * 5s poll rather than by a second timer of its own. */
let _recordRefreshPending = false;

/** True when `next` disagrees with the source the decks are actually holding.
 * A feature seen for the FIRST time (a fresh mount's `{}` -> populated) is not
 * a change: nothing was fetched or loaded under the old value yet, so there is
 * nothing stale to refresh -- this keeps the poll in AnalysisSourceToggle.svelte
 * from forcing a multi-MB /anlz refetch on every tick when nothing moved.
 *
 * Reads `analysisSourceState.deckFeatures`, NOT `.features`. The mirror is a
 * snapshot of what the daemon said; only `deckFeatures` is a statement about
 * what would actually be stale. */
function _decksDisagreeWith(next: Record<string, AnalysisSource>): boolean {
	for (const [feature, source] of Object.entries(next)) {
		const held = analysisSourceState.deckFeatures[feature];
		if (held !== undefined && held !== source) return true;
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
	for (const feature of Object.keys(intended)) {
		// `beatgrid` is the only feature exposed, and `beatgrid_source` is the
		// stamp for exactly that lane. Widening ANALYSIS_SOURCE_FEATURES means
		// teaching /anlz to stamp the new lane too, not defaulting it here.
		analysisSourceState.deckFeatures[feature] =
			served !== null && feature === 'beatgrid' ? served : intended[feature];
	}
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
		_recordDeckSources(features, null);
		if (isSuperseded()) return;
		analysisSourceState.features = features;
		return;
	}
	const served = await _refreshDecks(serialize);
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
	if (isSuperseded()) return;
	_latestPollAdopted = poll;
	const features = _featuresOf(body);
	await _adopt(features, isSuperseded, true);
	await _drainPendingRecordRefresh();
}

/** The UI feature map, plus the per-lane toggle capture, from one wire body.
 * Both readers need both halves, and reading the toggle only here keeps
 * `_lastToggles` in step with every answer the daemon has actually given. */
function _featuresOf(body: {
	lanes: Record<string, { effective: string; toggle: string }>;
}): Record<string, AnalysisSource> {
	const features: Record<string, AnalysisSource> = {};
	for (const feature of ANALYSIS_SOURCE_FEATURES) {
		const lane = body.lanes[_LANE_OF_FEATURE[feature]];
		if (lane === undefined) continue;
		features[feature] = _UI_SOURCE_OF_EFFECTIVE[lane.effective];
		_lastToggles[_LANE_OF_FEATURE[feature]] = lane.toggle;
	}
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
	// Captured BEFORE the PUT: this is the value the PUT is about to displace,
	// and the PUT's own response can only report the new one.
	const displacedToggle = _lastToggles[lane];
	const mutation = ++_latestMutation;
	const body = await unwrap(
		api.PUT('/api/v1/analysis/source', {
			body: { lane, toggle: _TOGGLE_OF_UI_SOURCE[source] }
		})
	);
	const features = _featuresOf(body);
	try {
		// `serialize: false`: the ONLY caller is the `analysis_source` performance
		// command (performance-ipc.svelte.ts), which already holds a claim on
		// [...DECK_IDS, 'sync'] for the whole of this call. Claiming those scopes a
		// second time from inside that claim would wait on its own tail forever.
		await _adopt(features, () => mutation !== _latestMutation, false);
	} catch (exc) {
		await _rollBackFailedSwitch(lane, displacedToggle, mutation);
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
 * else and owns the daemon now, so this stands down rather than clobbering it. */
async function _rollBackFailedSwitch(
	lane: string,
	displacedToggle: string | undefined,
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
	try {
		// The compensating PUT's own answer is ADOPTED, not discarded. The failed
		// switch's PUT already moved `_lastToggles[lane]` to the toggle it was
		// attempting, so leaving that in place would make a retry before the next
		// 5s poll capture the FAILED toggle as the thing to displace - and a
		// second failure would then "restore" the daemon to the source the decks
		// never reached, which is the exact split this function exists to undo
		// (discussion_r3970967286 P1 BLOCKING). Running the response through
		// `_featuresOf` is what keeps the capture in step with the daemon.
		_featuresOf(
			await unwrap(api.PUT('/api/v1/analysis/source', { body: { lane, toggle: displacedToggle } }))
		);
	} catch (exc) {
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
 * command queue is exactly the defect the runner exists to close. */
async function _refreshDecks(serialize: boolean): Promise<AnalysisSource | null> {
	let served: AnalysisSource | null;
	if (serialize) {
		if (_refreshRunner === null) {
			throw new Error('no analysis source refresh runner is installed');
		}
		served = await _refreshRunner(() => refreshAnalysisSourceDecks(DECK_IDS, deckStates));
		if (served === undefined) {
			// A runner that awaits the work but drops its result would silently
			// write `undefined` into the deck watermark, and every later
			// comparison against it would read "never refreshed". Loud, because
			// the runner is injected and TypeScript cannot enforce this at the
			// installation site's runtime.
			throw new Error('the analysis source refresh runner dropped its work result');
		}
	} else {
		served = await refreshAnalysisSourceDecks(DECK_IDS, deckStates);
	}
	// Any successful refresh refetches EVERY loaded deck, so it satisfies a
	// pending record-change retry whatever triggered it.
	_recordRefreshPending = false;
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
 * `kind: 'tracks'` is the only signal the daemon publishes for a completed
 * ingest refresh (ingest.py `_refresh_worker`), and it carries no ids, so this
 * refreshes every loaded deck rather than a subset. Returns the unsubscribe
 * for the caller's effect cleanup. */
export function subscribeAnalysisRecordChanges(): () => void {
	const unsubscribeKind = subscribeKind('tracks', _refreshOwnGridsAfterRecordChange);
	// The bus fires resync on reconnect, a sequence gap, a malformed frame and a
	// slow-consumer drop, and it replays NOTHING: `_fireResync` walks the resync
	// listeners only, never the kind listeners (events-bus.ts). Its own header
	// states the rule this subscription was breaking -- "a consumer that handles
	// a kind MUST also handle resync, otherwise it goes stale exactly when the
	// bus is least reliable" -- so a missed analysis-completion event left a
	// loaded OWN deck on the superseded grid indefinitely, while the bus was
	// explicitly reporting that events had been missed
	// (discussion_r3969942717 P1 BLOCKING). Same handler, because a resync says
	// "you missed something, refetch" and that is exactly what a `tracks` change
	// means here too. Disposed together with the kind subscription.
	const unsubscribeResync = subscribeResync(_refreshOwnGridsAfterRecordChange);
	return () => {
		unsubscribeKind();
		unsubscribeResync();
	};
}

function _refreshOwnGridsAfterRecordChange(): void {
	if (!_anyFeatureIsOwn()) return;
	_recordRefreshPending = true;
	void _drainPendingRecordRefresh();
}

/** Runs a pending record-change refresh, and LEAVES IT PENDING if it fails.
 *
 * Called on every poll as well as on the event itself, which is what makes the
 * retry real: the failure cannot be recovered by the ordinary source-change
 * path, because this refresh does not change the source, so
 * `_decksDisagreeWith` correctly sees nothing to do while the decks sit on a
 * superseded grid with the shared cache already emptied under them
 * (discussion_r3969942725 P1 BLOCKING). Reuses the existing 5s poll rather
 * than adding a second timer with its own lifecycle to leak. */
async function _drainPendingRecordRefresh(): Promise<void> {
	if (!_recordRefreshPending) return;
	try {
		await _refreshDecks(true);
	} catch (exc) {
		console.error(
			'[analysis-source] own-grid refresh after an analysis record change failed; ' +
				'still pending, retrying on the next poll',
			exc
		);
	}
}
