// requirement: REORDER-ROW-02
// [if] a drag-move drops in a playlist [then] the pane's rows and dividers move in place at once, with no reload of the pane and every row key stable, [else stop].
// [if] a drag hovers a gap [then] a 3 px accent line sits in that gap, inside a section when the gap is below its divider, [else stop].
// [if] the server refuses the move [then] rows, dividers and selection go back exactly as they were, [else stop].
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let local;
let layout;
let actions;
before(async () => {
	local = await loadTypeScriptModule('src/lib/library/entries-move-local.ts');
	layout = await loadTypeScriptModule('src/lib/library/playlist-sections.ts');
	actions = await loadTypeScriptModule('src/lib/library/section-actions.ts', { viteApiBase: 'http://daemon.test' });
});

/** Six membership rows a..f, 1-based like the pane loads them. */
function playlistRows() {
	return ['a', 'b', 'c', 'd', 'e', 'f'].map((id, index) => ({
		stable_id: `s-${id}`,
		item_id: id,
		order: index + 1
	}));
}

const ids = (rows) => rows.map((row) => row.item_id);

// ---------------------------------------------------------- optimistic move

test('a track drop reorders rows in place: same keys, unmoved rows keep their objects', () => {
	const rows = playlistRows();
	const result = local.moveEntriesLocally(rows, [], { item_ids: ['b'], section_ids: [], before_item_id: 'e' });
	assert.deepEqual(ids(result.rows), ['a', 'c', 'd', 'b', 'e', 'f']);
	assert.deepEqual(result.rows.map((row) => row.order), [1, 2, 3, 4, 5, 6], 'slots renumbered');
	assert.deepEqual(new Set(ids(result.rows)), new Set(ids(rows)), 'every each-key survives');
	assert.equal(result.rows[0], rows[0], 'a row whose slot did not change is the same object');
	assert.equal(result.rows[4], rows[4]);
});

test('no target means the end; a target inside the block means the line below it', () => {
	const rows = playlistRows();
	assert.deepEqual(
		ids(local.moveEntriesLocally(rows, [], { item_ids: ['a'], section_ids: [] }).rows),
		['b', 'c', 'd', 'e', 'f', 'a']
	);
	assert.deepEqual(
		ids(local.moveEntriesLocally(rows, [], { item_ids: ['b', 'c'], section_ids: [], before_item_id: 'c' }).rows),
		['a', 'b', 'c', 'd', 'e', 'f'],
		'a drop on the dragged rows themselves stays put'
	);
});

test('dividers follow the server layout: a track dropped into a section moves the boundary', () => {
	// | a b | S1 c d | S2 e f
	const sections = [
		{ id: 'S1', title: 'one', before_position: 2 },
		{ id: 'S2', title: 'two', before_position: 4 }
	];
	const intoS1 = local.moveEntriesLocally(playlistRows(), sections, {
		item_ids: ['f'],
		section_ids: [],
		before_item_id: 'c'
	});
	assert.deepEqual(ids(intoS1.rows), ['a', 'b', 'f', 'c', 'd', 'e']);
	assert.deepEqual(intoS1.sections.map((s) => [s.id, s.before_position]), [['S1', 2], ['S2', 5]]);
	const aboveS2 = local.moveEntriesLocally(playlistRows(), sections, {
		item_ids: ['a'],
		section_ids: [],
		before_section_id: 'S2'
	});
	assert.deepEqual(ids(aboveS2.rows), ['b', 'c', 'd', 'a', 'e', 'f'], 'lands at the end of S1');
	assert.deepEqual(aboveS2.sections.map((s) => [s.id, s.before_position]), [['S1', 1], ['S2', 4]]);
});

test('a section drags with its tracks as one block', () => {
	const sections = [
		{ id: 'S1', title: 'one', before_position: 2 },
		{ id: 'S2', title: 'two', before_position: 4 }
	];
	const result = local.moveEntriesLocally(playlistRows(), sections, {
		item_ids: ['e', 'f'],
		section_ids: ['S2'],
		before_item_id: 'a'
	});
	assert.deepEqual(ids(result.rows), ['e', 'f', 'a', 'b', 'c', 'd']);
	assert.deepEqual(result.sections.map((s) => [s.id, s.before_position]), [['S2', 0], ['S1', 4]]);
});

test('the selection follows its rows; revert restores rows, dividers and selection exactly', () => {
	const rows = playlistRows();
	const sections = [{ id: 'S1', title: 'one', before_position: 3 }];
	let shown = sections;
	const pane = { rows, selected_orders: [2], selected_order: 2 };
	const applied = local.applyOptimisticEntriesMove(pane, sections, (next) => (shown = next), {
		item_ids: ['b'],
		section_ids: [],
		before_item_id: 'e'
	});
	assert.deepEqual(ids(pane.rows), ['a', 'c', 'd', 'b', 'e', 'f']);
	assert.deepEqual(pane.selected_orders, [4], 'the moved row stays selected at its new slot');
	assert.equal(pane.selected_order, 4);
	assert.deepEqual(shown.map((s) => s.before_position), [2]);
	applied.revert();
	assert.equal(pane.rows, rows);
	assert.deepEqual(pane.selected_orders, [2]);
	assert.equal(pane.selected_order, 2);
	assert.equal(shown, sections);
});

test('control: a row without a membership id or an unknown line is not mirrored', () => {
	const pane = { rows: playlistRows(), selected_orders: [], selected_order: null };
	const rows = pane.rows;
	const noop = () => assert.fail('dividers must not change');
	assert.equal(local.applyOptimisticEntriesMove(pane, [], noop, { item_ids: ['zz'], section_ids: [] }), null);
	assert.equal(
		local.applyOptimisticEntriesMove(pane, [], noop, { item_ids: ['a'], section_ids: [], before_item_id: 'zz' }),
		null
	);
	pane.rows = [{ stable_id: 's', item_id: null, order: 1 }];
	assert.equal(local.applyOptimisticEntriesMove(pane, [], noop, { item_ids: ['a'], section_ids: [] }), null);
	pane.rows = rows;
});

// ------------------------------------------------------------ insertion line

test('the line sits centred in the gap the pointer opens (above or below the row midpoint)', () => {
	const rowHeight = 22;
	// The pointer in the top half of slot 3 opens gap 3; in its bottom half, gap 4.
	assert.equal(layout.gapFromPointer(3 * rowHeight + 5, rowHeight, 10), 3);
	assert.equal(layout.gapFromPointer(3 * rowHeight + 17, rowHeight, 10), 4);
	assert.equal(local.dropLineTop(3, rowHeight, 30), 30 + 3 * rowHeight + rowHeight / 2 - 1.5);
	assert.equal(local.DROP_LINE_PX, 3);
});

test('at a section boundary the line and the landing place agree', () => {
	const rowHeight = 22;
	// | a b | S1 c d : slots a0 b1 S1=2 c3 d4
	const rows = playlistRows().slice(0, 4);
	const headers = layout.arrangeSections(rows, [{ id: 'S1', title: 'one', before_position: 2 }], new Set()).headers;
	const header = headers[0];
	assert.equal(header.slot, 2);
	// Gap just below the divider: the drop lands first inside S1, and the line
	// sits below the divider's slot.
	assert.deepEqual(layout.dropTargetAt(header.slot + 1, headers, rows), { before_item_id: 'c' });
	assert.ok(local.dropLineTop(header.slot + 1, rowHeight, 0) > (header.slot + 1) * rowHeight);
	// Gap just above it: the drop lands at the end of the section above.
	assert.deepEqual(layout.dropTargetAt(header.slot, headers, rows), { before_section_id: 'S1' });
	assert.ok(local.dropLineTop(header.slot, rowHeight, 0) < (header.slot + 1) * rowHeight);
});

// --------------------------------------------- section-actions move (behaviour)

/** Drive the real sectionActions().move against a stubbed daemon and record
 * the order things happen in. `moveStatus` is the entries:move answer. */
async function runMove(moveStatus) {
	const events = [];
	const saved = globalThis.fetch;
	globalThis.fetch = async (input) => {
		const request = input instanceof Request ? input : new Request(input);
		const path = new URL(request.url).pathname;
		events.push(`${request.method} ${path}`);
		if (path.endsWith('/entries:move')) {
			return moveStatus === 200
				? Response.json({ playlist_id: 'pl', sections: [] })
				: Response.json({ detail: { code: 'CONFLICT', message: 'stale' } }, { status: moveStatus });
		}
		if (path.endsWith('/sections')) return Response.json({ owner_kind: 'playlist', rows: [] });
		throw new Error(`unexpected request ${request.method} ${path}`);
	};
	const ui = actions.sectionActions({
		editableListId: () => 'pl',
		rows: () => [],
		sections: () => [],
		selectedOrders: () => [],
		setRenaming: () => {},
		applyMoveLocally: () => {
			events.push('local move');
			return { revert: () => events.push('revert') };
		},
		refreshListQuietly: async () => {
			events.push('quiet refresh');
		},
		toast: (message, level) => events.push(`toast ${level}: ${message}`)
	});
	try {
		ui.move({ item_ids: ['b'], section_ids: [], before_item_id: 'e' });
		await new Promise((resolve) => setTimeout(resolve, 50));
	} finally {
		globalThis.fetch = saved;
	}
	return events;
}

test('a move shows locally before the request, then reads back quietly (no pane reload)', async () => {
	const events = await runMove(200);
	assert.equal(events[0], 'local move', 'rows move before the network round trip');
	assert.equal(events[1], 'POST /api/v1/playlists/pl/entries:move');
	assert.ok(events.includes('quiet refresh'));
	assert.ok(events.includes('GET /api/v1/playlists/pl/sections'));
	assert.ok(!events.includes('revert'));
	assert.ok(events.includes('toast info: Moved track'));
});

test('a refused move reverts, still reads back quietly, and says why', async () => {
	const events = await runMove(409);
	assert.deepEqual(events.slice(0, 3), ['local move', 'POST /api/v1/playlists/pl/entries:move', 'revert']);
	assert.ok(events.includes('quiet refresh'));
	assert.ok(events.some((event) => event.startsWith('toast error: section update failed')));
	assert.ok(!events.includes('toast info: Moved track'));
});

// The keyed {#each} (no remount) and the 3 px line are proved rendered, in
// TrackTable.stories.ts: MoveDropKeepsEveryRowNode, MoveDragDrawsTheInsertionLine,
// MoveDragLineClearsOnLeaveAndEnd, MoveDragLineInsideASection.
