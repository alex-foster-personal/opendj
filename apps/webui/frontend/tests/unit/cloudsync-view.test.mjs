/**
 * The /cloudsync UI's pure decisions (plan W17 + the UI half of W5), run
 * against the real module bundled by esbuild (no DOM, no mocks).
 *
 * Regression one-liners:
 * - if an unset policy cell reads anything but 'unset' (e.g. 'stream') then broken
 * - if a budget edit on an unset or non-cached cell yields a PUT then broken
 * - if picking 'unset' yields a PUT (there is no route that clears a row) then broken
 * - if the chip reads anything but 'off' without a fresh heartbeat then broken
 * - if Sync now posts without an effective hub URL then broken
 * - if the config form sends enabled=true with no hub URL then broken
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

function policy(overrides = {}) {
	return {
		machine_id: 'm1',
		asset_kind: 'audio',
		mode: 'pinned',
		cache_budget_mb: null,
		updated_at: '2026-09-11T00:00:00.000000+00:00',
		origin_device_id: 'm1',
		...overrides
	};
}

function status(overrides = {}) {
	return {
		enabled: false,
		configured: false,
		running: false,
		heartbeat_at: null,
		enabled_source: 'default',
		endpoint_source: 'default',
		reason: 'CloudSync is not configured.',
		signed_in_as: null,
		last_push_at: null,
		last_pull_at: null,
		last_result: null,
		rows_pending: null,
		endpoint: null,
		recent_results: [],
		update_required: null,
		...overrides
	};
}

function config(effective = {}, file = null) {
	return {
		path: '/tmp/x/cloudsync-config.json',
		file,
		effective: {
			enabled: false,
			hub_url: null,
			machine_name: null,
			configured: false,
			enabled_source: 'default',
			hub_url_source: 'default',
			...effective
		}
	};
}

// ----------------------------------------------------------- matrix

test('an unset cell reads unset with the budget locked', () => {
	/** if an absent policy row renders as any real mode (the old ?? 'stream') then broken */
	const cell = view.policyCellView(undefined);
	assert.equal(cell.mode, 'unset');
	assert.equal(cell.budgetEditable, false);
	assert.notEqual(cell.mode, 'stream');
});

test('a stored mode is shown verbatim and only cached unlocks the budget', () => {
	/** if a non-cached stored mode unlocks the budget, or cached does not, then broken */
	for (const mode of ['pinned', 'stream', 'excluded']) {
		const cell = view.policyCellView(policy({ mode }));
		assert.equal(cell.mode, mode);
		assert.equal(cell.budgetEditable, false, `${mode} must lock the budget`);
	}
	assert.equal(view.policyCellView(policy({ mode: 'cached', cache_budget_mb: 512 })).budgetEditable, true);
});

test('a budget edit on an unset cell is refused and writes nothing', () => {
	/** if a budget on an unset cell produces a PUT (the old path wrote mode stream) then broken */
	const decision = view.policyBudgetChange('m1', 'audio', undefined, '256');
	assert.equal(decision.kind, 'refuse');
	assert.match(decision.reason, /never saved as stream/);
});

test('a budget edit is refused unless the stored mode is cached', () => {
	/** if a budget edit on a pinned/stream/excluded cell yields a PUT then broken */
	for (const mode of ['pinned', 'stream', 'excluded']) {
		const decision = view.policyBudgetChange('m1', 'audio', policy({ mode }), '256');
		assert.equal(decision.kind, 'refuse', `${mode} must refuse a budget`);
	}
	const ok = view.policyBudgetChange('m1', 'audio', policy({ mode: 'cached' }), '256');
	assert.deepEqual(ok, {
		kind: 'put',
		body: { machine_id: 'm1', asset_kind: 'audio', mode: 'cached', cache_budget_mb: 256 }
	});
});

test('a malformed budget is refused on a cached cell', () => {
	/** if -1, 1.5 or text reaches the PUT body then broken */
	for (const raw of ['-1', '1.5', 'lots']) {
		assert.equal(view.policyBudgetChange('m1', 'audio', policy({ mode: 'cached' }), raw).kind, 'refuse');
	}
});

test('picking unset is refused, and a real mode sends no stray budget', () => {
	/** if 'unset' yields a PUT, or a non-cached mode carries a budget, then broken */
	const unsetPick = view.policyModeChange('m1', 'audio', policy(), 'unset');
	assert.equal(unsetPick.kind, 'refuse');
	// The refusal must say WHY (no route clears a row), not pass by accident
	// as an "unknown mode": that is the inert-with-tooltip contract.
	assert.match(unsetPick.reason, /no DELETE route.*not implemented - see PARITY-TODO/);
	assert.equal(view.policyModeChange('m1', 'audio', undefined, 'bogus').kind, 'refuse');
	const toStream = view.policyModeChange('m1', 'audio', policy({ mode: 'cached', cache_budget_mb: 64 }), 'stream');
	assert.equal(toStream.kind, 'put');
	assert.equal(toStream.body.mode, 'stream');
	assert.equal(toStream.body.cache_budget_mb, null);
	const toCached = view.policyModeChange('m1', 'audio', policy({ mode: 'cached', cache_budget_mb: 64 }), 'cached');
	assert.equal(toCached.body.cache_budget_mb, 64);
});

test('only runtime-wired asset kinds escape the inert PARITY-TODO marker', () => {
	/** if an unwired kind has no inert tooltip, or audio / karaoke_words is marked inert, then broken */
	// 6c5105e4d (test(cloud): cover CLOUDSYNC-10 believed-state acceptance gaps, #2660)
	// wired audio deck load to its stored policy via resolve_playback_source, and
	// moved 'audio' into RUNTIME_WIRED_ASSET_KINDS; audio now escapes the marker.
	assert.equal(view.inertKindTitle('karaoke_words'), null);
	assert.equal(view.inertKindTitle('audio'), null);
	for (const kind of ['stem_bundle', 'anlz_cache', 'vocal_cache', 'lyrics_cache']) {
		assert.match(view.inertKindTitle(kind), /not implemented - see PARITY-TODO/);
	}
});

// ----------------------------------------------------------- chip

test('the chip reads off with no fresh heartbeat, even when configured with an ok result', () => {
	/** if configured + last ok but running=false renders anything but off then broken */
	assert.equal(view.chipState(null), 'off');
	assert.equal(view.chipState(status()), 'off');
	const configuredNoBeat = status({
		configured: true,
		running: false,
		enabled: false,
		last_result: { status: 'ok', message: 'pushed 3' }
	});
	assert.equal(view.chipState(configuredNoBeat), 'off');
});

test('the chip lights only with a fresh heartbeat, and keeps inconclusive distinct', () => {
	/** if a running loop never leaves off, or inconclusive folds into ok, then broken */
	const live = { configured: true, running: true, enabled: true };
	assert.equal(view.chipState(status(live)), 'syncing');
	assert.equal(view.chipState(status({ ...live, last_result: { status: 'ok', message: '' } })), 'ok');
	assert.equal(view.chipState(status({ ...live, last_result: { status: 'error', message: '' } })), 'error');
	assert.equal(
		view.chipState(status({ ...live, last_result: { status: 'inconclusive', message: '' } })),
		'inconclusive'
	);
});

test('chipFullLabel matches the full status strings', () => {
	/** if the full label drifts from the chip's visible text at desktop widths then broken */
	const frozenNow = Date.parse('2026-09-11T12:00:00.000Z');
	const pushAt = '2026-09-11T11:48:00.000Z';
	const originalNow = Date.now;
	Date.now = () => frozenNow;
	try {
		assert.equal(view.chipFullLabel(null), 'sync: off');
		assert.equal(view.chipFullLabel(status()), 'sync: off');
		const live = { configured: true, running: true, enabled: true };
		assert.equal(view.chipFullLabel(status(live)), 'sync: syncing');
		assert.equal(
			view.chipFullLabel(
				status({
					...live,
					last_push_at: pushAt,
					last_result: { status: 'ok', message: '' }
				})
			),
			'sync: ok 12m ago'
		);
		assert.equal(
			view.chipFullLabel(status({ ...live, last_result: { status: 'error', message: 'fail' } })),
			'sync: error'
		);
		assert.equal(
			view.chipFullLabel(
				status({
					...live,
					last_push_at: pushAt,
					last_result: { status: 'inconclusive', message: '' }
				})
			),
			'sync: inconclusive 12m ago'
		);
	} finally {
		Date.now = originalNow;
	}
});

test('chipShortLabel is three letters or fewer for compact viewports', () => {
	/** if the short label is too long to stay single-line at 900px then broken */
	const live = { configured: true, running: true, enabled: true };
	assert.equal(view.chipShortLabel(null), 'off');
	assert.equal(view.chipShortLabel(status(live)), 'sync');
	assert.equal(
		view.chipShortLabel(status({ ...live, last_result: { status: 'ok', message: '' } })),
		'ok'
	);
	assert.equal(
		view.chipShortLabel(status({ ...live, last_result: { status: 'error', message: '' } })),
		'err'
	);
	assert.equal(
		view.chipShortLabel(status({ ...live, last_result: { status: 'inconclusive', message: '' } })),
		'inc'
	);
});

// requirement: CSSTATUS-04
// [if] a local in-progress 409 is presented [then] the summary names conflict and next step without raw host or HTTP 409, [else stop]
test('a local in-progress 409 gets a plain conflict summary with raw text only in details', () => {
	const raw =
		'a CloudSync sync (a Sync now or a scheduler round) is already running against this data dir';
	const error = new ApiError(409, 'CLOUDSYNC_SYNC_IN_PROGRESS', raw, new Response());
	const presented = view.presentCloudSyncError(error);
	assert.match(presented.summary, /conflict/i);
	assert.match(presented.summary, /wait|refresh|try again/i);
	assert.doesNotMatch(presented.summary, /HTTP 409/);
	assert.doesNotMatch(presented.summary, /http:\/\//i);
	assert.doesNotMatch(presented.ariaLabel, /HTTP 409/);
	assert.equal(presented.details, raw);
	assert.equal(presented.isConflict409, true);
});

// requirement: CSSTATUS-04
// [if] a journaled SyncDigestMismatch row is presented [then] the conflict summary is shown and the raw text stays in details, [else stop]
test('a journaled SyncDigestMismatch gets the remote conflict summary', () => {
	const raw = 'SyncDigestMismatch: local and hub digests disagree after sync';
	const presented = view.presentCloudSyncResultError({ status: 'error', message: raw });
	assert.equal(presented.summary, view.CLOUDSYNC_REMOTE_409_SUMMARY);
	assert.equal(presented.details, raw);
	assert.equal(presented.isConflict409, true);
});

// requirement: CSSTATUS-04
// [if] a transport-shaped HTTP 409 message is presented [then] the remote conflict summary is shown and the raw input stays in details, [else stop]
test('a transport-shaped HTTP 409 gets the remote conflict summary', () => {
	const raw = 'POST http://internal-hub.example/api/v1/sync -> HTTP 409: track_vendor_ids conflict';
	const presented = view.presentCloudSyncResultError({ status: 'error', message: raw });
	assert.equal(presented.summary, view.CLOUDSYNC_REMOTE_409_SUMMARY);
	assert.equal(presented.details, raw);
	assert.equal(presented.isConflict409, true);
});

// requirement: CSSTATUS-04
// [if] a non-409 error is presented [then] the generic failure summary is shown and raw text stays in details, [else stop]
test('a non-409 error gets a safe failure summary', () => {
	const raw = 'POST http://hub.example/api/v1/sync -> HTTP 502: hub unreachable';
	const presented = view.presentCloudSyncResultError({ status: 'error', message: raw });
	assert.equal(presented.summary, view.CLOUDSYNC_GENERIC_ERROR_SUMMARY);
	assert.equal(presented.details, raw);
	assert.equal(presented.isConflict409, false);
});

// requirement: CSSTATUS-04
// [if] the chip is in error [then] chipTitle uses the safe summary plus the CloudSync link CTA, [else stop]
test('chipTitle uses the safe error summary plus the quick-actions CTA', () => {
	const live = { configured: true, running: true, enabled: true };
	const raw = 'POST http://internal-hub.example/api/v1/sync -> HTTP 409: busy';
	const title = view.chipTitle(
		status({ ...live, last_result: { status: 'error', message: raw } }),
		null
	);
	assert.match(title, /CloudSync conflict:/);
	assert.match(title, /Click for quick actions\./);
	assert.doesNotMatch(title, /HTTP 409/);
	assert.doesNotMatch(title, /internal-hub/);
});

// requirement: CSSTATUS-04
// [if] the chip is in error [then] chipAriaLabel is descriptive, [else stop]
test('chipAriaLabel is descriptive for error and stable for non-error states', () => {
	const live = { configured: true, running: true, enabled: true };
	const raw = 'CLOUDSYNC_SYNC_IN_PROGRESS: already running';
	const errorStatus = status({ ...live, last_result: { status: 'error', message: raw } });
	assert.match(view.chipAriaLabel(errorStatus, null), /another sync is already running/);
	assert.equal(view.chipAriaLabel(status(live), null), 'CloudSync status');
	assert.equal(view.chipAriaLabel(null, null), 'CloudSync status');
});

// requirement: CSSTATUS-04
// [if] chip state helpers are unchanged [then] off/syncing/ok/error/inconclusive labels still match, [else stop]
test('chip state helpers still return the existing off/syncing/ok/error/inconclusive values', () => {
	const live = { configured: true, running: true, enabled: true };
	assert.equal(view.chipState(status(live)), 'syncing');
	assert.equal(
		view.chipState(status({ ...live, last_result: { status: 'ok', message: '' } })),
		'ok'
	);
	assert.equal(
		view.chipState(status({ ...live, last_result: { status: 'error', message: '' } })),
		'error'
	);
	assert.equal(view.chipShortLabel(status({ ...live, last_result: { status: 'error', message: '' } })), 'err');
	assert.equal(
		view.chipShortLabel(status({ ...live, last_result: { status: 'inconclusive', message: '' } })),
		'inc'
	);
});

test('chipTitle names the state and points at quick actions', () => {
	/** if the tooltip CTA still points at the old recent-results popover then broken */
	assert.equal(view.CHIP_HREF, '/cloudsync');
	assert.equal(view.CHIP_QUICK_ACTIONS_CTA, 'Click for quick actions.');
	const offTitle = view.chipTitle(status(), null);
	assert.match(offTitle, /CloudSync is off/);
	assert.match(offTitle, /Click for quick actions\./);
	assert.doesNotMatch(offTitle, /Click to open recent results\./);
	const live = { configured: true, running: true, enabled: true };
	const originalNow = Date.now;
	Date.now = () => Date.parse('2026-09-11T12:00:00.000Z');
	try {
		const okTitle = view.chipTitle(
			status({
				...live,
				last_push_at: '2026-09-11T11:48:00.000Z',
				last_result: { status: 'ok', message: '' }
			}),
			null
		);
		assert.match(okTitle, /CloudSync last succeeded 12m ago/);
		assert.match(okTitle, /Click for quick actions\./);
	} finally {
		Date.now = originalNow;
	}
});

// ----------------------------------------------------------- sync now + config

test('Sync now posts the effective hub URL and machine name, and refuses without a hub', () => {
	/** if Sync now fires with no hub URL, or ignores the effective (env-won) URL, then broken */
	const openGate = { appPosture: 'prep', uiMirror: null };
	assert.equal(view.syncNowRequest(null, openGate).kind, 'refuse');
	assert.equal(view.syncNowRequest(config(), openGate).kind, 'refuse');
	const decision = view.syncNowRequest(
		config({ hub_url: 'http://env-hub:8686', hub_url_source: 'env', machine_name: 'silver' }),
		openGate
	);
	assert.deepEqual(decision, {
		kind: 'post',
		body: { hub_url: 'http://env-hub:8686', name: 'silver', force: false }
	});
});

test('syncNowRequest refuses when gig posture gates sync', () => {
	const decision = view.syncNowRequest(
		config({ hub_url: 'http://hub:8686', configured: true }),
		{ appPosture: 'gig', uiMirror: null }
	);
	assert.equal(decision.kind, 'refuse');
	assert.match(decision.reason, /gig_posture/);
});

test('syncNowRequest refuses when a deck is playing', () => {
	const decision = view.syncNowRequest(
		config({ hub_url: 'http://hub:8686', configured: true }),
		{ appPosture: 'prep', uiMirror: { decks: { '1': { playing: true } } } }
	);
	assert.equal(decision.kind, 'refuse');
	assert.match(decision.reason, /deck_playing/);
});

test('forceSyncNowRequest posts force true and ignores gate', () => {
	const decision = view.forceSyncNowRequest(
		config({ hub_url: 'http://hub:8686', machine_name: 'silver', configured: true })
	);
	assert.deepEqual(decision, {
		kind: 'post',
		body: { hub_url: 'http://hub:8686', name: 'silver', force: true }
	});
});

// requirement: CSUI-02
// [if] a deck is playing [then] ordinary sync is refused and force sync still posts, [else stop]
test('quick-action labels and gate helpers stay shared with the status tab', () => {
	assert.equal(view.SYNC_NOW_LABEL, 'Sync now');
	assert.equal(view.REFRESH_STATUS_LABEL, 'Refresh status');
	assert.equal(view.ADVANCED_OPTIONS_LABEL, 'Advanced options');
	const playingGate = {
		appPosture: 'prep',
		uiMirror: { decks: { '1': { playing: true } } }
	};
	const ordinary = view.syncNowRequest(
		config({ hub_url: 'http://hub:8686', machine_name: 'silver', configured: true }),
		playingGate
	);
	assert.equal(ordinary.kind, 'refuse');
	assert.match(ordinary.reason, /deck_playing/);
	const forced = view.forceSyncNowRequest(
		config({ hub_url: 'http://hub:8686', machine_name: 'silver', configured: true })
	);
	assert.equal(forced.kind, 'post');
	assert.equal(forced.body.force, true);
});

test('the config form mirrors the backend validator', () => {
	/** if enabled=true with no hub, or a non-http hub, reaches the PUT then broken */
	assert.equal(view.configPutBody({ enabled: true, hubUrl: ' ', machineName: '' }).kind, 'refuse');
	assert.equal(view.configPutBody({ enabled: false, hubUrl: 'ftp://x', machineName: '' }).kind, 'refuse');
	assert.deepEqual(view.configPutBody({ enabled: true, hubUrl: ' http://h:1 ', machineName: '  ' }), {
		kind: 'put',
		body: { enabled: true, hub_url: 'http://h:1', machine_name: null }
	});
});

// requirement: CSUI-01
// if /cloudsync tab state stops following the URL query then broken
test('cloudSyncTabFromUrl maps tab query params to the visible tab', () => {
	const tab = (query) => view.cloudSyncTabFromUrl(new URL(`http://localhost/cloudsync${query}`));
	assert.equal(tab('?tab=policies'), 'policies');
	assert.equal(tab('?tab=pins'), 'pins');
	assert.equal(tab('?tab=overview'), 'overview');
	assert.equal(tab('?tab=fleet'), 'fleet');
	assert.equal(tab(''), 'status');
	assert.equal(tab('?tab=nope'), 'status');
});

// ----------------------------------------------------------- status headline
// requirement: CSSTATUS-05
// if a raw connection-refused exception ever renders as a bare "error" with
// no cause and no next step then broken

test('plainSyncFailureCause names common transport failures and never invents unknown ones', () => {
	/** if an unrecognized message is dropped instead of falling back then broken */
	assert.match(
		view.plainSyncFailureCause('POST http://h:1/api/v1/sync/hello failed: [Errno 61] Connection refused'),
		/could not reach the hub machine \(connection refused\)/
	);
	assert.match(view.plainSyncFailureCause('Read timed out'), /did not respond in time \(timeout\)/);
	assert.match(
		view.plainSyncFailureCause('getaddrinfo ENOTFOUND hub.example'),
		/hub address could not be found \(DNS lookup failed\)/
	);
	assert.match(view.plainSyncFailureCause('401 Unauthorized'), /rejected the sign-in/);
	assert.match(view.plainSyncFailureCause('some brand new exception text'), /last sync attempt failed/);
	assert.match(
		view.plainSyncFailureCause('POST https://hub:8870/api/v1/sync/push -> HTTP 502:  (after 45.0s)'),
		/502, after 45s/
	);
});

test('an unreachable hub on a live loop is a wait, never a red error (#3870)', () => {
	/** if a live loop waiting for its hub renders the red Not synced headline then broken */
	const waiting = status({
		configured: true,
		running: true,
		reason: `${view.WAITING_FOR_HUB_PREFIX}: could not reach the hub machine. Sync retries in the background.`,
		last_result: { status: 'error', message: 'could not reach the hub machine: [Errno 111] Connection refused' }
	});
	assert.equal(view.isWaitingForHub(waiting), true);
	const headline = view.statusHeadline(waiting);
	assert.equal(headline.tone, 'warn');
	assert.match(headline.text, /^Waiting for hub: could not reach the hub machine/);
	assert.doesNotMatch(headline.text, /Not synced/);
	assert.equal(view.chipState(waiting), 'syncing');

	/** if the same error without the backend's wait reason stops reading as an error then broken */
	const notWaiting = status({ ...waiting, reason: null });
	assert.equal(view.isWaitingForHub(notWaiting), false);
	assert.equal(view.statusHeadline(notWaiting).tone, 'error');
	assert.equal(view.chipState(notWaiting), 'error');

	/** if a dead loop with the wait reason on file reads as syncing then broken */
	const deadLoop = status({ ...waiting, running: false });
	assert.equal(view.isWaitingForHub(deadLoop), false);
	assert.equal(view.chipState(deadLoop), 'off');
});

test('statusHeadline leads with a plain sentence and a next step for every state', () => {
	/** if an error result renders a bare word with no cause and no next step then broken */
	const errorHeadline = view.statusHeadline(
		status({
			configured: true,
			running: true,
			last_result: { status: 'error', message: '[Errno 61] Connection refused' }
		})
	);
	assert.equal(errorHeadline.tone, 'error');
	assert.match(errorHeadline.text, /^Not synced: could not reach the hub machine/);
	assert.match(errorHeadline.text, /Sync now/);

	/** if "not configured" ever reads as a bare no/off with no next step then broken */
	const notConfigured = view.statusHeadline(status());
	assert.equal(notConfigured.tone, 'off');
	assert.match(notConfigured.text, /not set up/);
	assert.match(notConfigured.text, /Enter a hub URL/);

	/** if configured-but-not-running collapses into the same text as not-configured then broken */
	const noHeartbeat = view.statusHeadline(status({ configured: true, running: false }));
	assert.equal(noHeartbeat.tone, 'warn');
	assert.match(noHeartbeat.text, /not running automatically/);
	assert.notEqual(noHeartbeat.text, notConfigured.text);

	/** Sol review, PR #2604: an OK result must win over a stale heartbeat,
	 * never "not running automatically" -- if this regresses then broken */
	const okDespiteStaleHeartbeat = view.statusHeadline(
		status({
			configured: true,
			running: false,
			last_push_at: '2026-09-14T11:00:00.000Z',
			last_result: { status: 'ok', message: '' }
		})
	);
	assert.equal(okDespiteStaleHeartbeat.tone, 'ok');
	assert.match(okDespiteStaleHeartbeat.text, /^In sync\./);

	/** Devin review, PR #2604: a saved endpoint with automatic sync off is
	 * "manual only", never the same "not set up" text as no endpoint at all
	 * -- if it reads identically to notConfigured then broken */
	const manualOnly = view.statusHeadline(
		status({ configured: false, endpoint: 'http://hub:8686', endpoint_source: 'file' })
	);
	assert.equal(manualOnly.tone, 'off');
	assert.match(manualOnly.text, /Automatic sync is off/);
	assert.match(manualOnly.text, /http:\/\/hub:8686/);
	assert.notEqual(manualOnly.text, notConfigured.text);

	/** Devin review, PR #2604: disabling CloudSync after a recorded error
	 * must not leave the headline red -- current config wins over a stale
	 * journal verdict; if this still reads "Not synced" then broken */
	const disabledAfterError = view.statusHeadline(
		status({
			configured: false,
			endpoint: null,
			last_result: { status: 'error', message: 'Connection refused' }
		})
	);
	assert.equal(disabledAfterError.tone, 'off');
	assert.doesNotMatch(disabledAfterError.text, /Not synced/);

	/** if an inconclusive result reads as ok or as error then broken */
	const inconclusive = view.statusHeadline(
		status({ configured: true, running: true, last_result: { status: 'inconclusive', message: '' } })
	);
	assert.equal(inconclusive.tone, 'warn');
	assert.match(inconclusive.text, /could not fully confirm/);

	/** if a genuine ok result still shows jargon instead of "In sync" then broken */
	const frozenNow = Date.parse('2026-09-14T12:00:00.000Z');
	const originalNow = Date.now;
	Date.now = () => frozenNow;
	try {
		const ok = view.statusHeadline(
			status({
				configured: true,
				running: true,
				last_push_at: '2026-09-14T11:48:00.000Z',
				last_result: { status: 'ok', message: '' }
			})
		);
		assert.equal(ok.tone, 'ok');
		assert.equal(ok.text, 'In sync. Last synced 12m ago.');
	} finally {
		Date.now = originalNow;
	}
});

// REQ: CLOUDSYNC-16
test('identityBacklogNote states the consequence and hands over no command (#3252)', () => {
	/** if a zero or missing backlog still shows a note then broken */
	assert.equal(view.identityBacklogNote(null), null);
	assert.equal(view.identityBacklogNote(0), null);

	const many = view.identityBacklogNote(7331);

	/** if the count is missing then the note is not reporting the backlog at all */
	assert.match(many, /7331 tracks/);

	/** if a human-facing panel prints a command for a person to run then broken:
	 * the whole defect in #3252 was handing the user a CLI invocation. */
	assert.doesNotMatch(many, /python -m/);
	assert.doesNotMatch(many, /--live/);
	assert.doesNotMatch(many, /--for-hub/);

	/** if the note never says what is degraded then it cannot be prioritized or
	 * safely ignored, which is what left the user unable to act on it */
	assert.match(many, /duplicate detection/i);
	assert.match(many, /relinking/i);

	/** if it does not say sync is unaffected then it keeps reading as a sync
	 * failure under the green heading, which is the other half of #3252 */
	assert.match(many, /unaffected/i);

	/** if it does not say which machine can do the work then the reader is left
	 * guessing why the hub cannot */
	assert.match(many, /holds your music/i);
	assert.match(many, /never on the hub/i);

	/** if singular phrasing is not grammatical for a count of one then broken */
	const one = view.identityBacklogNote(1);
	assert.match(one, /1 track /);
	assert.doesNotMatch(one, /1 tracks/);
	assert.match(one, /is waiting/);
	assert.doesNotMatch(one, /are waiting/);
});

test('env overrides are named when they mask the saved config', () => {
	/** if an env-won field is silently shown as the saved value then broken */
	assert.deepEqual(view.envOverrideNotes(config()), []);
	const notes = view.envOverrideNotes(
		config({ enabled: true, enabled_source: 'env', hub_url: 'http://e:1', hub_url_source: 'env' })
	);
	assert.equal(notes.length, 2);
	assert.match(notes[0], /MDT_CLOUDSYNC_SCHEDULER/);
	assert.match(notes[1], /MDT_CLOUDSYNC_HUB_URL/);
});

// requirement: CSSTATUS-06
// [if] status.update_required is set [then] chipState is update_required and labels mention install the latest Open DJ
test('update_required renders a dedicated chip state and install copy', () => {
	const live = { configured: true, running: true, enabled: true };
	const updateRequired = {
		code: 'SYNC_WIRE_VERSION',
		local_wire_version: 3,
		peer_wire_version: 4,
		action: 'install the latest Open DJ'
	};
	const wireMismatch = status({ ...live, update_required: updateRequired });
	assert.equal(view.chipState(wireMismatch), 'update_required');
	assert.equal(view.chipFullLabel(wireMismatch), 'sync: update required');
	assert.equal(view.chipShortLabel(wireMismatch), 'upd');
	assert.match(view.chipTitle(wireMismatch, null), /install the latest Open DJ/);
	assert.match(view.chipTitle(wireMismatch, null), /this machine speaks v3/);
	assert.match(view.chipAriaLabel(wireMismatch, null), /install the latest Open DJ/);
	const headline = view.statusHeadline(wireMismatch);
	assert.equal(headline.tone, 'warn');
	assert.match(headline.text, /App update required to sync/);
	assert.match(headline.text, /Install the latest Open DJ/);
});

// requirement: CSSTATUS-06
// [if] status has connection refused error only [then] chipState is error and copy does not mention update required
test('connection refused stays a generic error without update required copy', () => {
	const live = { configured: true, running: true, enabled: true };
	const networkError = status({
		...live,
		update_required: null,
		last_result: { status: 'error', message: '[Errno 61] Connection refused' }
	});
	assert.equal(view.chipState(networkError), 'error');
	assert.equal(view.chipFullLabel(networkError), 'sync: error');
	const headline = view.statusHeadline(networkError);
	assert.equal(headline.tone, 'error');
	assert.doesNotMatch(headline.text, /update required/i);
	assert.doesNotMatch(view.chipTitle(networkError, null), /update required/i);
});
