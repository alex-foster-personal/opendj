import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * PLAYS-9745 remainder: a History night opens in the track table with a Played at column.
 *
 * [if] a night id does not round-trip or a bad date is accepted [then] the pane opens the wrong night - broken.
 * [if] the night's rows are not its plays in play order with played_at [then] the table is not the night - broken.
 * [if] Played at is not the local HH:MM, or shows outside a History night [then] the column misleads - broken.
 * [if] Played at is not a sort key the sort IPC accepts [then] agents cannot sort by it - broken.
 * [if] browser_select_playlist refuses a night id [then] agents cannot open a night in the table - broken.
 */

let night;
let pane;
let sort;
let widths;
let tips;
let guards;
let wire;
let captured;

before(async () => {
	night = await loadTypeScriptModule('src/lib/history/history-night-pane.ts');
	pane = await loadTypeScriptModule('src/lib/components/rb/browser/pane-contract.svelte.ts');
	sort = await loadTypeScriptModule('src/lib/components/rb/browser/browser-sort-ipc.ts');
	widths = await loadTypeScriptModule('src/lib/rb/library-column-widths.ts');
	tips = await loadTypeScriptModule('src/lib/rb/column-tips.ts');
	guards = await loadTypeScriptModule('src/lib/rb/performance-ipc-guards.ts');
	wire = await loadTypeScriptModule('src/lib/components/rb/browser/browser-row-wire.ts');
	const fixture = fileURLToPath(
		new URL('./fixtures/playlist-pending-availability-captured.json', import.meta.url)
	);
	captured = JSON.parse(readFileSync(fixture, 'utf8')).find(
		(r) => r.file_availability === 'present'
	);
});

const play = (position, stable_id, played_at) => ({
	position,
	stable_id,
	play_id: `p${position}`,
	played_at,
	deck: 1,
	source: 'opendj'
});

describe('History night in the track table', () => {
	it('addresses a night as history:YYYY-MM-DD and refuses anything else', () => {
		assert.equal(night.historyNightId('2026-11-13'), 'history:2026-11-13');
		assert.equal(night.historyNightDate('history:2026-11-13'), '2026-11-13');
		assert.equal(night.historyNightDate('history:13-11-2026'), null);
		assert.equal(night.historyNightDate('pl-1'), null);
		assert.equal(night.historyNightDate(null), null);
		assert.throws(() => night.historyNightId('tonight'), /not a YYYY-MM-DD night/);
		const node = night.historyNightNode('2026-11-13', 3);
		assert.deepEqual(
			[node.playlist_id, node.name, node.kind, node.track_count],
			['history:2026-11-13', 'History Fri 2026-11-13', 'history', 3]
		);
	});

	it('lists the plays in play order with played_at, repeats kept, gone tracks left out', () => {
		const plays = [
			play(1, 'a', '2026-11-13T22:05:00Z'),
			play(2, 'gone', '2026-11-13T22:09:00Z'),
			play(3, 'b', '2026-11-13T22:14:00Z'),
			play(4, 'a', '2026-11-14T01:30:00Z')
		];
		const items = [
			{ ...captured, stable_id: 'a', title: 'First' },
			{ ...captured, stable_id: 'b', title: 'Second' }
		];
		const rows = night.historyNightRows(plays, items, (item, order) =>
			wire.rowFromIndexWire(item, order)
		);
		assert.deepEqual(
			rows.map((r) => [r.stable_id, r.order, r.played_at]),
			[
				['a', 1, '2026-11-13T22:05:00Z'],
				['b', 2, '2026-11-13T22:14:00Z'],
				['a', 3, '2026-11-14T01:30:00Z']
			]
		);
		assert.equal(rows[1].title, 'Second');
	});

	it('shows the local HH:MM, and only while the rows are plays', () => {
		assert.equal(
			night.playedAtCellText({ played_at: '2026-11-14T01:30:00Z' }, 'Europe/London'),
			'01:30'
		);
		assert.equal(
			night.playedAtCellText({ played_at: '2026-11-14T01:30:00Z' }, 'America/New_York'),
			'20:30'
		);
		assert.equal(night.playedAtCellText({ played_at: null }), '');
		assert.equal(night.playedAtCellText({}), '');
		assert.equal(night.rowsArePlays([{ played_at: null }, {}]), false);
		assert.equal(night.rowsArePlays([{}, { played_at: '2026-11-14T01:30:00Z' }]), true);
		assert.equal(widths.COL_DEFAULTS.played_at > 0, true);
		assert.ok(tips.COLUMN_TIPS.played_at.length > 20);
	});

	it('sorts by Played at as a sort key the sort IPC accepts, in time order', () => {
		assert.ok(sort.BROWSER_SORT_KEYS.includes('played_at'));
		const rows = [
			{ stable_id: 'late', order: 1, played_at: '2026-11-14T01:30:00Z' },
			{ stable_id: 'none', order: 2, played_at: null },
			{ stable_id: 'early', order: 3, played_at: '2026-11-13T22:05:00Z' }
		];
		const ids = (dir) => pane.sortRows(rows, 'played_at', dir).map((r) => r.stable_id);
		assert.deepEqual(ids(1), ['early', 'late', 'none']);
		assert.deepEqual(ids(-1), ['late', 'early', 'none']);
	});

	it('lets agents open a night through browser_select_playlist', () => {
		assert.deepEqual(
			guards._parseCommand({ type: 'browser_select_playlist', playlist_id: 'history:2026-11-13' }),
			{ type: 'browser_select_playlist', playlist_id: 'history:2026-11-13' }
		);
	});
});
