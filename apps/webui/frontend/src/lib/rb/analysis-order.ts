// analysis-order.ts -- the pure decision logic behind AnalysisDotsPopover's
// "click to queue" affordance, extracted so it can be unit tested directly
// (a .svelte file cannot be unit tested - same reasoning as WaveRow.svelte's
// paint-position.ts split, pin 53ba89ca8ddc on #1288).
//
// Sol review on #1291 (PRRT_kwDOSEvNd86fkmIn): the existing popover tests
// asserted on the component's SOURCE TEXT (string/ordering checks), which
// cannot catch a real behavioural regression - specifically, that an
// ingest-refresh response excluding the clicked step must never be reported
// as queued. Extracting the transport-boundary decision here lets that be
// asserted directly: stub only startIngestRefresh, and check upsertJob is
// never invoked on the excluded path.

import { RbApiError } from './api-rb-error';
import type { AnalysisOrder } from './api-ingest';

// Re-exported so tests/unit/analysis-order.test.mjs can throw a REAL
// instance of the class this module's own `instanceof` check reads -
// the test bundler builds each entry point separately, so an
// RbApiError imported through a different bundle would fail the
// instanceof check inside this file, not just in the test's own
// assertions (see api-smartlists.test.mjs's note on the same limit).
export { RbApiError };

type AnalysisOrderDeps = {
	orderTrackAnalysis: () => Promise<AnalysisOrder>;
	/** Called only after the shared track-order command succeeds. */
	upsertJob: (phase: AnalysisOrder['phase']) => void;
	toast: (message: string, kind: 'info' | 'error') => void;
};

/** Orders exactly one track and analysis kind, then reports the server state. */
export async function runAnalysisOrder(kind: string, deps: AnalysisOrderDeps): Promise<void> {
	let order: AnalysisOrder;
	try {
		order = await deps.orderTrackAnalysis();
	} catch (e) {
		if (e instanceof RbApiError && e.status === 409) {
			deps.toast('An analysis job is already running', 'info');
		} else if (e instanceof RbApiError && e.status === 422) {
			deps.toast('This analysis cannot be ordered - configure ingest first', 'error');
		} else {
			deps.toast(`Queue failed: ${e instanceof Error ? e.message : String(e)}`, 'error');
		}
		return;
	}
	deps.upsertJob(order.phase);
	deps.toast(`Queued ${kind} analysis for this track`, 'info');
}
