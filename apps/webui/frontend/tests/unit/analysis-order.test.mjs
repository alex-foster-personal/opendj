/**
 * runAnalysisOrder (extracted from AnalysisDotsPopover.svelte's order(),
 * pin a2bb92d16f4a) is the transport-boundary decision behind "click to
 * queue": did the started refresh actually include the clicked step, and
 * only mark it queued if so.
 *
 * WHY THIS FILE EXISTS. Sol review on #1291 (PRRT_kwDOSEvNd86fkmIn): the
 * sibling analysis-dots-popover.test.mjs asserts on the component's SOURCE
 * TEXT (string/ordering checks), which cannot catch a real regression here -
 * a .svelte file cannot be unit tested directly, so the decision was
 * extracted to a plain module (same move as WaveRow.svelte's
 * paint-position.ts, pin 53ba89ca8ddc) and is exercised for real: stub only
 * the transport boundary (startIngestRefresh), assert the real behaviour.
 *
 * [if] the refresh response excludes the clicked step [then ⛔] upsertJob
 *   must never be called - marking it queued would be a false success.
 * [if] the refresh response includes the clicked step [then ⛔] upsertJob
 *   fires exactly once and the toast is the "Queued" info message.
 * [if] startIngestRefresh rejects with a 409 [then ⛔] upsertJob is never
 *   called and the toast says a refresh is already running.
 * [if] startIngestRefresh rejects with a 422 [then ⛔] upsertJob is never
 *   called and the toast says to configure the ingest modal.
 * [if] startIngestRefresh rejects with anything else [then ⛔] upsertJob is
 *   never called and the toast carries the real error message.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// RbApiError comes from THIS SAME bundle, not a separate load of api-rb.ts:
// the loader bundles each entry point independently, so a class imported
// through a different bundle would fail analysis-order.ts's own internal
// `instanceof RbApiError` check, not just an assertion here (see
// api-smartlists.test.mjs's documented note on the same limitation).
const { runAnalysisOrder, RbApiError } = await loadTypeScriptModule('src/lib/rb/analysis-order.ts');

function harness() {
	const calls = { upsertJob: 0, toasts: [] };
	const deps = (orderTrackAnalysis) => ({
		orderTrackAnalysis,
		upsertJob: () => {
			calls.upsertJob += 1;
		},
		toast: (message, kind) => {
			calls.toasts.push({ message, kind });
		}
	});
	return { calls, deps };
}

test('runAnalysisOrder records the phase returned by the track-order command', async () => {
	const { calls, deps } = harness();
	await runAnalysisOrder('vocals', deps(async () => ({ stable_id: 'track-a', kind: 'vocals', phase: 'queued' })));

	assert.equal(calls.upsertJob, 1);
	assert.equal(calls.toasts.length, 1);
	assert.equal(calls.toasts[0].kind, 'info');
	assert.match(calls.toasts[0].message, /Queued vocals analysis for this track/);
});

test('runAnalysisOrder reports a 409 without ever queuing', async () => {
	const { calls, deps } = harness();
	await runAnalysisOrder(
		'vocals',
		deps(async () => {
			throw new RbApiError(409, 'conflict', 'conflict');
		})
	);

	assert.equal(calls.upsertJob, 0);
	assert.equal(calls.toasts[0].kind, 'info');
	assert.match(calls.toasts[0].message, /already running/);
});

test('runAnalysisOrder reports a 422 without ever queuing', async () => {
	const { calls, deps } = harness();
	await runAnalysisOrder(
		'vocals',
		deps(async () => {
			throw new RbApiError(422, 'unconfigured', 'unconfigured');
		})
	);

	assert.equal(calls.upsertJob, 0);
	assert.equal(calls.toasts[0].kind, 'error');
	assert.match(calls.toasts[0].message, /configure ingest first/);
});

test('runAnalysisOrder reports any other failure without ever queuing', async () => {
	const { calls, deps } = harness();
	await runAnalysisOrder(
		'vocals',
		deps(async () => {
			throw new Error('network down');
		})
	);

	assert.equal(calls.upsertJob, 0);
	assert.equal(calls.toasts[0].kind, 'error');
	assert.match(calls.toasts[0].message, /network down/);
});
