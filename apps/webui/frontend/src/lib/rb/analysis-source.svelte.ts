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
import { subscribeKind } from '$lib/api/events-bus';
import { engine } from './audio-engine.svelte';

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
	features: Record<string, AnalysisSource>;
}

export const analysisSourceState = $state<AnalysisSourceState>({ features: {} });

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
// stale RBX highlight then read as "no change" to `_hasLiveChange` and left
// the decks where they were for good (discussion_r3968534418 P1 BLOCKING).
// Stamp every GET and never adopt one older than the newest already adopted.
let _latestPollIssued = 0;
let _latestPollAdopted = 0;

/** True when `next` disagrees with a feature this module already had a
 * confirmed value for. A feature seen for the FIRST time (a fresh mount's
 * `{}` -> populated) is not a change: nothing was fetched or loaded under
 * the old value yet, so there is nothing stale to refresh -- this keeps the
 * poll in AnalysisSourceToggle.svelte from forcing a multi-MB /anlz refetch
 * on every tick when nothing actually moved. */
function _hasLiveChange(next: Record<string, AnalysisSource>): boolean {
	for (const [feature, source] of Object.entries(next)) {
		const previous = analysisSourceState.features[feature];
		if (previous !== undefined && previous !== source) return true;
	}
	return false;
}

/** Adopts a confirmed selection from the daemon (a GET mirror or a PUT
 * response) and, only when a feature actually changed value, forces the
 * loaded decks and shared ANLZ cache off the pre-switch payload -- see
 * engine.refreshDecksForAnalysisSourceChange (audio-engine.svelte.ts) for why
 * that is not automatic: the same /anlz URL now returns different bytes, and
 * neither the deck's terminal state nor the one-fetch-per-session cache nor
 * the response's own 1h HTTP cache header would otherwise notice
 * (discussion_r3921666943). Runs for a poll-detected external PUT exactly
 * like a local one -- an agent driving the endpoint directly must get the
 * same live effect as clicking the UI control.
 *
 * `analysisSourceState.features` is only written AFTER the refresh settles,
 * not before: if a loaded deck's cache-bypassing /anlz refresh rejects (a
 * transient network error, an ANALYSIS_NOT_FOUND from the new source), the
 * local mirror must stay on the OLD value so `_hasLiveChange` still sees a
 * disagreement against the next poll's `next` and retries the refresh --
 * publishing the new value before the refresh is confirmed would make that
 * poll compare next-against-next, read no change, and leave the failed
 * deck's stale pre-switch anlz in place indefinitely while the toggle
 * reports the new source (discussion_r3921666943 follow-up). Throws through
 * to the caller (setAnalysisSource/loadAnalysisSource) on failure -- no
 * silent partial adoption.
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
	if (_hasLiveChange(features)) await _refreshDecks(serialize);
	if (isSuperseded()) return;
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
	const features: Record<string, AnalysisSource> = {};
	for (const feature of ANALYSIS_SOURCE_FEATURES) {
		const lane = body.lanes[_LANE_OF_FEATURE[feature]];
		if (lane === undefined) continue;
		features[feature] = _UI_SOURCE_OF_EFFECTIVE[lane.effective];
	}
	await _adopt(features, isSuperseded, true);
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
	const mutation = ++_latestMutation;
	const body = await unwrap(
		api.PUT('/api/v1/analysis/source', {
			body: { lane: _LANE_OF_FEATURE[feature], toggle: _TOGGLE_OF_UI_SOURCE[source] }
		})
	);
	const features: Record<string, AnalysisSource> = {};
	for (const featureName of ANALYSIS_SOURCE_FEATURES) {
		const lane = body.lanes[_LANE_OF_FEATURE[featureName]];
		if (lane === undefined) continue;
		features[featureName] = _UI_SOURCE_OF_EFFECTIVE[lane.effective];
	}
	// `serialize: false`: the ONLY caller is the `analysis_source` performance
	// command (performance-ipc.svelte.ts), which already holds a claim on
	// [...DECK_IDS, 'sync'] for the whole of this call. Claiming those scopes a
	// second time from inside that claim would wait on its own tail forever.
	await _adopt(features, () => mutation !== _latestMutation, false);
}

// ------------------------------------------------------- serialized refresh

/** Runs `work` under the performance command scheduler's all-deck-plus-sync
 * scopes. Injected rather than imported: performance-ipc.svelte.ts owns the
 * one scheduler and already imports setAnalysisSource from here, so importing
 * it back would close a module cycle. Same install-a-port shape
 * `installAuthoritativeAnlzGridSink` and `installScopedSyncRunner` use. */
export type AnalysisSourceRefreshRunner = (work: () => Promise<void>) => Promise<void>;

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
async function _refreshDecks(serialize: boolean): Promise<void> {
	if (!serialize) {
		await engine.refreshDecksForAnalysisSourceChange();
		return;
	}
	if (_refreshRunner === null) {
		throw new Error('no analysis source refresh runner is installed');
	}
	await _refreshRunner(() => engine.refreshDecksForAnalysisSourceChange());
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
	return subscribeKind('tracks', () => {
		if (!_anyFeatureIsOwn()) return;
		void _refreshDecks(true);
	});
}
