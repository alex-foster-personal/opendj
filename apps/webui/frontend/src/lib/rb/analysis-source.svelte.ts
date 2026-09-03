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

/** Pulls the daemon's current per-feature selection. Call on mount: the
 * server is authoritative and an agent may have changed it since. */
export async function loadAnalysisSource(): Promise<void> {
	const body = await unwrap(api.GET('/api/v1/analysis-source'));
	analysisSourceState.features = body.features;
}

/** Sets one feature's source. Fails loudly (throws) on a rejected feature
 * name or a daemon error -- no optimistic local write, since a silently
 * unapplied switch is exactly the defect PARITY-02 exists to prevent. */
export async function setAnalysisSource(
	feature: AnalysisSourceFeature,
	source: AnalysisSource
): Promise<void> {
	const body = await unwrap(api.PUT('/api/v1/analysis-source', { body: { feature, source } }));
	analysisSourceState.features = body.features;
}
