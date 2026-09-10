/**
 * PARITY-02 analysis source state: the shared rune, kept in a module with NO
 * dependency on audio-engine.svelte.ts.
 *
 * Split out of analysis-source.svelte.ts (which needs DECK_IDS/deckStates
 * from audio-engine.svelte.ts for its own poll/adopt logic) so a reader that
 * only needs to CHECK the current source - beatgrid-upgrade.ts, gating a
 * stale async response after a source switch (discussion_r3973991956 P1
 * BLOCKING) - does not also import the whole audio engine. Without this
 * split, beatgrid-upgrade.ts -> analysis-source.svelte.ts ->
 * audio-engine.svelte.ts -> anlz-cache.svelte.ts -> beatgrid-lazy.ts ->
 * beatgrid-upgrade.ts (its own lazy loader) closes a real import cycle.
 *
 * .svelte.ts extension is REQUIRED for the $state rune (RECON-FRONTEND 10.1).
 */

export type AnalysisSource = 'rekordbox' | 'own';

/** Features with a genuine own-rolled counterpart to A/B against rekordbox.
 * Mirrors the one lane apps.analysis.lanes' 5 lanes that this UI exposes --
 * widen only once another lane grows a real own-rolled counterpart. */
export const ANALYSIS_SOURCE_FEATURES = ['beatgrid'] as const;
export type AnalysisSourceFeature = (typeof ANALYSIS_SOURCE_FEATURES)[number];

interface AnalysisSourceState {
	/** What the DAEMON last told us it has selected. Drives the visible control. */
	features: Record<string, AnalysisSource>;
	/** What the loaded DECKS are actually holding. See `_recordDeckSources`
	 * in analysis-source.svelte.ts. Published rather than kept module-private
	 * because the two can legitimately disagree for a poll interval, and an
	 * operator or agent asking "why is this deck on the other grid" needs to
	 * read both halves, not infer one. */
	deckFeatures: Record<string, AnalysisSource>;
}

export const analysisSourceState = $state<AnalysisSourceState>({
	features: {},
	deckFeatures: {}
});
