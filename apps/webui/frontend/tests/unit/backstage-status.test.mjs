// requirement: BACKSTAGE-06
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let st;
const COMPONENT = readFileSync(
	new URL('../../src/routes/admin/StatusOverview.svelte', import.meta.url),
	'utf8'
);
const NOW = new Date('2026-10-13T12:00:00Z');
const ok = (value) => ({ kind: 'ok', value });

function healthy(overrides = {}) {
	return {
		health: ok({ status: 'ok', tracks: 10, tracksPlayable: 9 }),
		writeback: ok({ enabled: true, title: '' }),
		dedup: ok({ undecided: 0, total: 2, note: null }),
		missing: ok({ warming: false, broken: 0, total: 10 }),
		perf: ok({ latestTs: '2026-10-10', kpis: 74 }),
		farm: ok({ latestTs: '2026-10-12T09:00:00Z', kpis: 17 }),
		now: NOW,
		...overrides
	};
}
const byId = (rows) => Object.fromEntries(rows.map((r) => [r.id, r]));

before(async () => {
	st = await loadTypeScriptModule('src/routes/admin/status-rows.ts');
});

test('all healthy: every area is ok and collapses into one All clear line', () => {
	const view = st.splitStatus(st.deriveStatusRows(healthy()));
	assert.equal(view.attention.length, 0);
	assert.equal(
		st.allClearLine(view.clear),
		'All clear: Health, Flags, Missing tracks, Dedup, Performance KPIs, Stem farm'
	);
});

test('KPI data older than 7 days warns; exactly 7 does not', () => {
	const rows = byId(
		st.deriveStatusRows(
			healthy({
				perf: ok({ latestTs: '2026-10-06', kpis: 74 }),
				farm: ok({ latestTs: '2026-08-20T20:24:58Z', kpis: 17 })
			})
		)
	);
	assert.equal(rows.perf.state, 'ok', '7 days is not stale');
	assert.equal(rows.farm.state, 'warn');
	assert.equal(rows.farm.href, '/admin?p=farm');
	const age = rows.farm.line.find((s) => typeof s === 'object');
	assert.equal(age.n, '53');
	assert.match(age.title, /days since the latest run/);
	assert.equal(
		st.deriveStatusRows(healthy({ perf: ok({ latestTs: null, kpis: 74 }) }))[4].state,
		'warn'
	);
	assert.equal(
		st.deriveStatusRows(healthy({ perf: ok({ latestTs: 'nope', kpis: 74 }) }))[4].state,
		'err'
	);
});

test('writeback refused, missing tracks and undecided dedup clusters each warn with a link', () => {
	const rows = byId(
		st.deriveStatusRows(
			healthy({
				writeback: ok({
					enabled: false,
					title: 'sync to rekordbox disabled - one-way import only'
				}),
				missing: ok({ warming: false, broken: 3, total: 10 }),
				dedup: ok({ undecided: 2, total: 5, note: null })
			})
		)
	);
	assert.equal(rows.writeback.state, 'warn');
	assert.equal(rows.writeback.href, '/admin?p=flags');
	assert.match(rows.writeback.line.join(''), /Rekordbox writeback is refused/);
	assert.equal(rows.missing.state, 'warn');
	assert.equal(rows.missing.href, '/reconcile');
	assert.equal(rows.dedup.state, 'warn');
	assert.equal(rows.dedup.href, '/dedup');
	for (const row of [rows.missing, rows.dedup]) {
		const nums = row.line.filter((s) => typeof s === 'object');
		assert.equal(nums.length, 2, 'every number is a titled segment');
		for (const seg of nums) assert.ok(seg.title.length > 0);
	}
});

test('loading is idle, a failed fetch is err, an unhealthy daemon warns; worst first', () => {
	const view = st.splitStatus(
		st.deriveStatusRows(
			healthy({
				health: { kind: 'error', message: 'HTTP 500' },
				dedup: { kind: 'loading' },
				missing: ok({ warming: true }),
				perf: ok({ latestTs: '2026-01-01', kpis: 1 })
			})
		)
	);
	assert.deepEqual(
		view.attention.map((r) => [r.id, r.state]),
		[
			['health', 'err'],
			['perf', 'warn'],
			['missing', 'idle'],
			['dedup', 'idle']
		]
	);
	assert.equal(
		st.deriveStatusRows(
			healthy({ health: ok({ status: 'degraded', tracks: 0, tracksPlayable: 0 }) })
		)[0].state,
		'warn'
	);
	assert.equal(
		st.deriveStatusRows(
			healthy({ dedup: ok({ undecided: 0, total: 0, note: 'No duplicate scan has run yet.' }) })
		)[3].state,
		'idle'
	);
	assert.equal(st.allClearLine([]), null);
});

test('areas with no data source are inert with the PARITY-TODO tooltip, never a value', () => {
	assert.ok(st.INERT_AREAS.length > 0);
	for (const a of st.INERT_AREAS) assert.equal(a.title, 'not implemented - see PARITY-TODO');
	assert.match(COMPONENT, /aria-disabled="true" title=\{a\.title\}/);
});

test('component: W/A/S/D move focus via navArrowKey, text fields keep the key, no audio import', () => {
	assert.match(COMPONENT, /moveRowFocus\(event, list, 'a\[data-status-row\]'\)/);
	const nav = readFileSync(
		new URL('../../src/lib/shell/backstage-nav.ts', import.meta.url),
		'utf8'
	);
	assert.match(nav, /isTextEntryTarget\(event\.target\)/);
	assert.match(nav, /navArrowKey\(event\.key\)/);
	assert.doesNotMatch(COMPONENT, /audio-engine|performance-ipc/);
	// Gothic keeps the admin state colors, from the one shared token sheet.
	assert.match(COMPONENT, /import '\.\/admin-state\.css'/);
	assert.match(COMPONENT, /class="panel status admin-st"/);
	assert.doesNotMatch(COMPONENT, /--st-ok:/);
	const css = readFileSync(
		new URL('../../src/routes/admin/admin-state.css', import.meta.url),
		'utf8'
	);
	assert.match(
		css,
		/data-skin='mono-dev'\] \.admin-st \{\s*--st-ok: #6fbf8a;\s*--st-warn: #d9a441;\s*--st-err: #e0463e;/
	);
	assert.match(COMPONENT, /class="num"\s+title=\{seg\.title\}/);
});
