/**
 * PARITY-02: rbx-vs-own analysis source toggle -- reactive client for
 * GET/PUT /api/v1/analysis-source.
 *
 * Deliberately NOT localStorage-backed, unlike prefs.svelte.ts: this is a
 * TESTING/DEV affordance whose default is always 'rekordbox' and which must
 * NOT survive a relaunch. The daemon (apps.webui.server.analysis_source) is
 * the single source of truth; this module just mirrors it into a $state rune
 * so the TopBar dropdown and any other reader stay in sync with what the
 * server actually has selected, including a change an agent made directly
 * over the endpoint.
 *
 * .svelte.ts extension is REQUIRED for the $state rune (RECON-FRONTEND 10.1).
 */
import { api, unwrap } from '../api/client';
import { engine } from './audio-engine.svelte';

export type AnalysisSource = 'rekordbox' | 'own';

/** Features with a genuine own-rolled counterpart to A/B against rekordbox.
 * Mirrors apps.webui.server.analysis_source.FEATURES -- widen only once the
 * backend actually grows another same-shape own-rolled lane. */
export const ANALYSIS_SOURCE_FEATURES = ['beatgrid'] as const;
export type AnalysisSourceFeature = (typeof ANALYSIS_SOURCE_FEATURES)[number];

interface AnalysisSourceState {
	features: Record<string, AnalysisSource>;
}

export const analysisSourceState = $state<AnalysisSourceState>({ features: {} });

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
 * same live effect as clicking the UI control. */
async function _adopt(features: Record<string, AnalysisSource>): Promise<void> {
	const changed = _hasLiveChange(features);
	analysisSourceState.features = features;
	if (changed) await engine.refreshDecksForAnalysisSourceChange();
}

/** Pulls the daemon's current per-feature selection. Call on mount and on a
 * poll: the server is authoritative and an agent may have changed it since
 * (AnalysisSourceToggle.svelte's poll is what makes the latter visible). */
export async function loadAnalysisSource(): Promise<void> {
	const body = await unwrap(api.GET('/api/v1/analysis-source'));
	await _adopt(body.features);
}

/** Sets one feature's source. Fails loudly (throws) on a rejected feature
 * name or a daemon error -- no optimistic local write, since a silently
 * unapplied switch is exactly the defect PARITY-02 exists to prevent. */
export async function setAnalysisSource(
	feature: AnalysisSourceFeature,
	source: AnalysisSource
): Promise<void> {
	const body = await unwrap(api.PUT('/api/v1/analysis-source', { body: { feature, source } }));
	await _adopt(body.features);
}
