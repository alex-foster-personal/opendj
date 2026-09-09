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
 * silent partial adoption. */
async function _adopt(features: Record<string, AnalysisSource>, mutation: number | null): Promise<void> {
	if (mutation !== null && mutation !== _latestMutation) return;
	const changed = _hasLiveChange(features);
	if (changed) await engine.refreshDecksForAnalysisSourceChange();
	if (mutation !== null && mutation !== _latestMutation) return;
	analysisSourceState.features = features;
}

/** Pulls the daemon's current per-feature selection. Call on mount and on a
 * poll: the server is authoritative and an agent may have changed it since
 * (AnalysisSourceToggle.svelte's poll is what makes the latter visible). */
export async function loadAnalysisSource(): Promise<void> {
	const mutation = _latestMutation;
	const body = await unwrap(api.GET('/api/v1/analysis/source'));
	if (mutation !== _latestMutation) return;
	const features: Record<string, AnalysisSource> = {};
	for (const feature of ANALYSIS_SOURCE_FEATURES) {
		const lane = body.lanes[_LANE_OF_FEATURE[feature]];
		if (lane === undefined) continue;
		features[feature] = _UI_SOURCE_OF_EFFECTIVE[lane.effective];
	}
	await _adopt(features, mutation);
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
	await _adopt(features, mutation);
}
