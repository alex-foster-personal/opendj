// requirement: BACKSTAGE-07
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let kt;
const TODAY = '2026-10-13';
const def = (label, direction = 'lower_better', unit = 'ms') => ({
	label,
	unit,
	direction,
	title: `${label} title`
});
const snap = (ts, values) => ({ ts, label: ts, values, provenance: {}, notes: null });

before(async () => {
	kt = await loadTypeScriptModule('src/routes/admin/kpi-table.ts');
});

const KPIS = {
	flat_one: def('Flat one'),
	small_move: def('Small move'),
	big_move: def('Big move'),
	single: def('Single reading'),
	never: def('Never measured'),
	never2: def('Also never')
};
const SNAPS = [
	snap('2026-09-01T00:00:00Z', {
		flat_one: 5,
		small_move: 100,
		big_move: 10,
		single: null,
		never: null
	}),
	snap('2026-10-10T00:00:00Z', {
		flat_one: 5,
		small_move: 90,
		big_move: 30,
		single: 7,
		never: null
	})
];

test('moved rows first, largest absolute move first, unmoved keep ledger order', () => {
	const t = kt.buildKpiTable(KPIS, SNAPS, TODAY);
	assert.deepEqual(
		t.rows.map((r) => r.metric),
		['big_move', 'small_move', 'flat_one', 'single']
	);
	assert.equal(t.rows[0].move.text, '▲ 200% worse');
	assert.equal(t.rows[1].move.text, '▼ 10% better');
});

test('never-measured rows fold out of the table rows', () => {
	const t = kt.buildKpiTable(KPIS, SNAPS, TODAY);
	assert.deepEqual(
		t.never.map((r) => r.metric),
		['never', 'never2']
	);
	assert.ok(t.rows.every((r) => r.latest !== null));
});

test('filter matches label or key, case-insensitive, and applies to the fold', () => {
	assert.deepEqual(
		kt.buildKpiTable(KPIS, SNAPS, TODAY, 'BIG').rows.map((r) => r.metric),
		['big_move']
	);
	assert.deepEqual(
		kt.buildKpiTable(KPIS, SNAPS, TODAY, 'small_').rows.map((r) => r.metric),
		['small_move']
	);
	const n = kt.buildKpiTable(KPIS, SNAPS, TODAY, 'also');
	assert.equal(n.rows.length, 0);
	assert.equal(n.never.length, 1);
	assert.equal(kt.buildKpiTable(KPIS, SNAPS, TODAY, '  ').rows.length, 4);
});

test('moves of 1000% or more read as xN better/worse', () => {
	assert.equal(kt.describeMove(10, 250, 'bad', 'ms').text, '▲ x25 worse');
	assert.equal(kt.describeMove(220, 52.4, 'good', 'ms').text, '▼ 76% better');
	assert.equal(kt.describeMove(1000, 1, 'good', 'ms').text, '▼ 100% better');
	assert.equal(kt.describeMove(0, 3, 'bad', 'ms').magnitude, Infinity);
	assert.equal(kt.describeMove(null, 3, 'flat', 'ms').text, 'one reading');
	assert.doesNotMatch(kt.describeMove(1, 5000, 'bad', 'ms').text, /%/);
});

test('rows older than 7 days are stale; 7 days is not', () => {
	const r = kt.buildKpiRow('small_move', KPIS.small_move, SNAPS, '2026-10-17');
	assert.equal(r.ageDays, 7);
	assert.equal(r.stale, false);
	assert.equal(kt.buildKpiRow('small_move', KPIS.small_move, SNAPS, '2026-10-18').stale, true);
});

test('ledger staleness gives the newest run date and its age', () => {
	const s = kt.ledgerStaleness(SNAPS, TODAY);
	assert.equal(s.lastDay, '2026-10-10');
	assert.equal(s.ageDays, 3);
	assert.equal(s.stale, false);
	assert.equal(kt.ledgerStaleness(SNAPS, '2026-11-01').stale, true);
	assert.equal(kt.ledgerStaleness([], TODAY).lastDay, null);
});

test('component: W/A/S/D route through navArrowKey and a text field wins', () => {
	const src = readFileSync(
		new URL('../../src/routes/admin/KpiTable.svelte', import.meta.url),
		'utf8'
	);
	assert.match(src, /moveRowFocus\(event, body, 'tr\[data-kpi-row\]'\)/);
	assert.match(src, /import '\.\/admin-state\.css'/);
	assert.doesNotMatch(src, /--st-ok:/);
	assert.doesNotMatch(src, /audio-engine|performance-ipc|api-rb/);
});
