// requirement: SEARCH-14509
/**
 * #12988: command search reaches ratings sync status and a ratings sync dry
 * run through the typed API client, never a live write, and a missing
 * ratings-diff.csv reads as such rather than as an empty dry run.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://ratings-sync.example.test';
const DIFF_PATH = '/tmp/sync/ratings-diff.csv';

let actions;
let search;
let cmdk;
let catalog;
let originalFetch;

function jsonResponse(body) {
	return new Response(JSON.stringify(body), {
		status: 200,
		headers: { 'content-type': 'application/json' }
	});
}

function statusBody(mtime) {
	return {
		sync_dir: '/tmp/sync',
		writeback_enabled: false,
		artefacts: { 'ratings-diff.csv': { path: DIFF_PATH, mtime } }
	};
}

/** Answers status with the given diff mtime and the dry run with `dryRun`, recording each request. */
function stubService(mtime, dryRun) {
	const calls = [];
	globalThis.fetch = async (input) => {
		const url = new URL(input.url);
		const body = input.method === 'POST' ? await input.clone().json() : null;
		calls.push({ method: input.method, path: url.pathname, body });
		if (url.pathname === '/api/v1/rb-djay-sync/status') return jsonResponse(statusBody(mtime));
		if (url.pathname === '/api/v1/rb-djay-sync/ratings/apply') return jsonResponse(dryRun);
		throw new Error(`unexpected request ${input.method} ${url.pathname}`);
	};
	return calls;
}

/** The first action row command search offers for `query`, filtered and ranked as CommandBarV2 does. */
function commandSearchRow(query) {
	const found = search.filterSettings(query, {
		hideTodo: true,
		showDev: false,
		group: null
	}).keyword;
	const rows = found
		.filter((def) => cmdk.actionableSetting(def) && cmdk.statelessSetting(def))
		.map((def) => ({
			kind: 'setting',
			id: def.id,
			label: def.label,
			path: '',
			def,
			value: '',
			next: null
		}));
	return cmdk.rankSettings(rows, query).find((row) => row.def.control.kind === 'action')?.def;
}

before(async () => {
	actions = await loadTypeScriptModule('src/lib/settings/ratings-sync-actions.ts', {
		viteApiBase: API_BASE
	});
	search = await loadTypeScriptModule('src/lib/settings/search.ts');
	cmdk = await loadTypeScriptModule('src/lib/rb/cmdk-actions.ts');
	catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('ratings sync status from command search reads the status probe only', async () => {
	/** [if] "ratings sync status" is searched and run [then] only GET status is sent and its diff path and gate are told, [else stop]. */
	const row = commandSearchRow('ratings sync status');
	assert.equal(row?.id, 'sync.ratings_sync_status');
	assert.deepEqual(row.control, { kind: 'action', action: 'ratings_sync_status' });
	assert.equal(cmdk.statelessSetting(row), true);
	assert.equal(cmdk.settingVerb(row, '', []), 'Enter to run');
	const calls = stubService(1_762_000_000, null);
	const message = await actions.runSettingAction(row.control.action);
	assert.deepEqual(
		calls.map((c) => `${c.method} ${c.path}`),
		['GET /api/v1/rb-djay-sync/status']
	);
	assert.equal(
		message,
		`Ratings diff at ${DIFF_PATH}, updated 2025-11-01 12:26 UTC. Rekordbox writeback gate is off.`
	);
});

test('ratings sync dry run from command search posts live off and counts resolutions', async () => {
	/** [if] "ratings dry run" is searched and run [then] the apply POST carries dry_run true, live false and rows are counted, [else stop]. */
	const row = commandSearchRow('ratings dry run');
	assert.equal(row?.id, 'sync.run_ratings_writeback');
	assert.deepEqual(row.control, { kind: 'action', action: 'ratings_sync_dry_run' });
	const calls = stubService(1_762_000_000, {
		dry_run: true,
		row_count: 4,
		summary: { rb_to_djay: 3, conflict: 1 }
	});
	const message = await actions.runSettingAction(row.control.action);
	assert.deepEqual(
		calls.map((c) => `${c.method} ${c.path}`),
		['GET /api/v1/rb-djay-sync/status', 'POST /api/v1/rb-djay-sync/ratings/apply']
	);
	assert.deepEqual(calls[1].body, {
		dry_run: true,
		live: false,
		cautious: false,
		bulk: false,
		i_understand_the_risks: false
	});
	assert.equal(
		message,
		'Ratings sync dry run: 4 rows (conflict 1, rb_to_djay 3). Nothing was written.'
	);
	const live = catalog.SETTINGS_CATALOG.filter(
		(def) => /ratings/.test(def.id) && def.control.kind === 'link'
	);
	assert.deepEqual(live, [], 'no ratings row may still link to a raw API path');
});

test('a dry run answer that is not a dry run is refused, not reported as one', async () => {
	/** [if] the apply endpoint answers dry_run false [then] the action rejects with that reason, [else stop]. */
	stubService(1_762_000_000, { dry_run: false, row_count: 4 });
	await assert.rejects(
		actions.runSettingAction('ratings_sync_dry_run'),
		/did not answer as a dry run/
	);
});

test('no ratings diff artifact is told truthfully by status and dry run', async () => {
	/** [if] status reports ratings-diff.csv with mtime null [then] both actions name the missing artifact and the dry run never posts, [else stop]. */
	let calls = stubService(null, null);
	const status = await actions.runSettingAction('ratings_sync_status');
	assert.match(
		status,
		new RegExp(`^No ratings diff artifact at ${DIFF_PATH} yet: build one with the metadata plan`)
	);
	calls = stubService(null, null);
	const dryRun = await actions.runSettingAction('ratings_sync_dry_run');
	assert.deepEqual(
		calls.map((c) => c.method),
		['GET']
	);
	assert.equal(
		dryRun,
		`Ratings sync dry run did not run: there is no ratings diff artifact at ${DIFF_PATH}; ` +
			'build one with the metadata plan (POST /api/v1/rb-djay-sync/metadata/plan). Nothing was written.'
	);
});
