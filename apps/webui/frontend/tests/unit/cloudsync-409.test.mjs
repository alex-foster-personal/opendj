/**
 * CloudSync 409 presentation gaps (issue #13047): an unknown 409 payload and a
 * success after a prior conflict, run against the real cloudsync-view module.
 *
 * Regression one-liners:
 * - if an ApiError 409 with an unknown code reads as a generic failure or leaks HTTP 409 then broken
 * - if a sync that succeeds after a conflict still shows conflict copy or the err chip then broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let view;
let ApiError;

before(async () => {
	view = await loadTypeScriptModule('src/lib/components/cloudsync/cloudsync-view.ts');
	const client = await loadTypeScriptModule('src/lib/api/client.ts');
	ApiError = client.ApiError;
});

/** Build a live CloudSync status with the given last_result. */
function liveStatus(lastResult) {
	return {
		enabled: true,
		configured: true,
		running: true,
		heartbeat_at: null,
		enabled_source: 'default',
		endpoint_source: 'default',
		reason: null,
		signed_in_as: null,
		last_push_at: null,
		last_pull_at: null,
		last_result: lastResult,
		rows_pending: null,
		endpoint: null,
		recent_results: [],
		update_required: null
	};
}

// requirement: CSSTATUS-04
// [if] CloudSync returns an unknown 409 payload [then] the conflict summary names the conflict and next step with raw text only in details, [else stop]
test('an unknown 409 payload still gets the conflict summary and keeps raw text in details', () => {
	/** An ApiError 409 with an unrecognised code falls back to the remote conflict copy. */
	const raw = 'unexpected body from http://internal-hub.example: {"weird":true}';
	const error = new ApiError(409, 'SOMETHING_NEW', raw, new Response());
	const presented = view.presentCloudSyncError(error);
	assert.equal(presented.summary, view.CLOUDSYNC_REMOTE_409_SUMMARY);
	assert.match(presented.summary, /try again/i);
	assert.doesNotMatch(presented.ariaLabel, /HTTP 409|internal-hub/);
	assert.equal(presented.details, raw);
	assert.equal(presented.isConflict409, true);
});

// requirement: CSSTATUS-04
// [if] CloudSync succeeds after a prior conflict [then] the chip and result drop the conflict copy and err state, [else stop]
test('a success after a prior conflict clears the conflict copy and the err chip', () => {
	/** The newest ok result replaces an earlier 409 in the chip and result presentation. */
	const conflict = { status: 'error', message: 'POST http://hub.example -> HTTP 409: busy' };
	assert.equal(view.chipState(liveStatus(conflict)), 'error');
	const ok = { status: 'ok', message: 'pushed 3 rows' };
	const after = liveStatus(ok);
	assert.equal(view.chipState(after), 'ok');
	assert.notEqual(view.chipShortLabel(after), 'err');
	assert.equal(view.chipAriaLabel(after, null), 'CloudSync status');
	assert.doesNotMatch(view.chipTitle(after, null), /conflict/i);
	const presented = view.presentCloudSyncResultError(ok);
	assert.equal(presented.isConflict409, false);
	assert.equal(presented.summary, 'pushed 3 rows');
});
