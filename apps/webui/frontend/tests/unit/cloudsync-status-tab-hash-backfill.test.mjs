// requirement: CLOUDSYNC-12139 / issue #11291: hash-pending backlog is enrichment, not sync failure
//
// [if] hash_pending is positive [then] the Status tab renders a distinct secondary block, [else stop]
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

function source(relative) {
	return readFileSync(fileURLToPath(new URL(`../../src/${relative}`, import.meta.url)), 'utf8');
}

const TAB_VIEW = source('lib/components/cloudsync/CloudSyncStatusTabView.svelte');
const TAB = source('lib/components/cloudsync/CloudSyncStatusTab.svelte') + TAB_VIEW;

const VIEW_ENTRY = [
	"export { default as CloudSyncStatusTabView } from '$lib/components/cloudsync/CloudSyncStatusTabView.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

let view;
let ssr;

before(async () => {
	view = await loadTypeScriptModule('src/lib/components/cloudsync/cloudsync-view.ts');
	ssr = await loadSvelteSsrModule(VIEW_ENTRY);
});

function healthyStatus(over = {}) {
	return {
		enabled: true,
		configured: true,
		running: true,
		heartbeat_at: '2026-10-14T21:04:00Z',
		enabled_source: 'file',
		endpoint_source: 'file',
		reason: null,
		signed_in_as: 'dj@example.com',
		last_push_at: '2026-10-14T21:03:40Z',
		last_pull_at: '2026-10-14T21:03:41Z',
		last_result: { status: 'ok', message: 'pushed 12, pulled 3' },
		rows_pending: 0,
		hash_pending: 7917,
		quarantined: 0,
		excluded_total: 4,
		endpoint: 'http://box.example-tailnet.ts.net:8686',
		recent_results: [],
		update_required: null,
		...over
	};
}

function baseViewProps(backlogNote) {
	return {
		status: healthyStatus(),
		config: null,
		loadError: null,
		notice: null,
		form: {
			enabled: true,
			hubUrl: 'http://box.example-tailnet.ts.net:8686',
			machineName: 'gig-laptop'
		},
		saving: false,
		syncing: false,
		syncDecision: { kind: 'refuse', reason: 'story only' },
		forceSyncDecision: { kind: 'refuse', reason: 'story only' },
		showForceSync: false,
		backlogNote,
		technicalDetailsLabel: 'Technical details',
		forceSyncLabel: 'Force sync',
		presentCloudSyncResultError: view.presentCloudSyncResultError,
		envOverrideNotes: view.envOverrideNotes,
		syncNow: () => {},
		forceSyncNow: () => {},
		refresh: () => {},
		saveConfig: async () => {}
	};
}

/** [if] hash_pending > 0 and sync ok [then] backlog is secondary, not a warn headline, [else stop] */
test('hash-pending backlog avoids headline warn styling when sync is healthy', () => {
	assert.doesNotMatch(
		TAB_VIEW,
		/data-testid="cloudsync-identity-backlog-note"[\s\S]{0,120}class="headline warn"/
	);
	assert.match(TAB_VIEW, /data-testid="cloudsync-hash-backfill-secondary"/);
	assert.match(TAB_VIEW, /class="secondary-copy"/);
});

/** [if] hash_pending is 7917 [then] a distinct secondary block renders with the note, [else stop] */
test('positive hash_pending SSR-renders the secondary enrichment block', () => {
	const backlogNote = view.identityBacklogNote(7917);
	assert.ok(backlogNote);
	const { body } = ssr.render(ssr.CloudSyncStatusTabView, { props: baseViewProps(backlogNote) });
	assert.match(body, /data-testid="cloudsync-hash-backfill-secondary"/);
	assert.match(body, /data-testid="cloudsync-identity-backlog-note"/);
	assert.match(body, /7917 tracks/);
	assert.doesNotMatch(
		body,
		/class="headline warn"[^>]*data-testid="cloudsync-identity-backlog-note"/
	);
	const syncHeadline = view.statusHeadline(healthyStatus());
	assert.equal(syncHeadline.tone, 'ok');
});

/** [if] fingerprint backfill never ran [then] copy names degraded duplicate detection and relinking, [else stop] */
test('identity backlog note names degraded duplicate detection and relinking', () => {
	const note = view.identityBacklogNote(42);
	assert.match(note, /duplicate detection/i);
	assert.match(note, /relinking/i);
	assert.match(note, /42 tracks/);
	assert.match(note, /unaffected/i);
});

/** [if] hash_pending is zero or null [then] no backlog secondary item renders, [else stop] */
test('zero or missing hash_pending hides the backlog secondary block', () => {
	assert.equal(view.identityBacklogNote(0), null);
	assert.equal(view.identityBacklogNote(null), null);
	const { body } = ssr.render(ssr.CloudSyncStatusTabView, { props: baseViewProps(null) });
	assert.doesNotMatch(body, /data-testid="cloudsync-hash-backfill-secondary"/);
	assert.doesNotMatch(body, /data-testid="cloudsync-identity-backlog-note"/);
	assert.match(TAB, /identityBacklogNote\(identityBacklogCount\)/);
});
