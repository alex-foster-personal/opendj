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

before(async () => {
	view = await loadTypeScriptModule('src/lib/components/cloudsync/cloudsync-view.ts');
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
	/** if an unwired kind has no inert tooltip, or karaoke_words is marked inert, then broken */
	assert.equal(view.inertKindTitle('karaoke_words'), null);
	for (const kind of ['audio', 'stem_bundle', 'anlz_cache', 'vocal_cache', 'lyrics_cache']) {
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

test('chipTitle names the state and links to /cloudsync', () => {
	/** if the tooltip CTA still points at the old popover then broken */
	assert.equal(view.CHIP_HREF, '/cloudsync');
	const offTitle = view.chipTitle(status(), null);
	assert.match(offTitle, /CloudSync is off/);
	assert.match(offTitle, /Click to open CloudSync\./);
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
		assert.match(okTitle, /Click to open CloudSync\./);
	} finally {
		Date.now = originalNow;
	}
});

// ----------------------------------------------------------- sync now + config

test('Sync now posts the effective hub URL and machine name, and refuses without a hub', () => {
	/** if Sync now fires with no hub URL, or ignores the effective (env-won) URL, then broken */
	assert.equal(view.syncNowRequest(null).kind, 'refuse');
	assert.equal(view.syncNowRequest(config()).kind, 'refuse');
	const decision = view.syncNowRequest(
		config({ hub_url: 'http://env-hub:8686', hub_url_source: 'env', machine_name: 'silver' })
	);
	assert.deepEqual(decision, { kind: 'post', body: { hub_url: 'http://env-hub:8686', name: 'silver' } });
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
// requirement: CSSTATUS-04
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
