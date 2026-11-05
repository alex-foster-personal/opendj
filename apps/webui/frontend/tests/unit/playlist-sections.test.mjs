/**
 * Dividers in the track table (R-SEC-1..3, R-DND-1, #7603, #7605). The hard veto
 * of #7605 is here: a drop must land where the gap showed.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const sections = await loadTypeScriptModule('src/lib/library/playlist-sections.ts');
const motion = await loadTypeScriptModule('src/lib/library/section-motion.ts');

const row = (order) => ({ order, item_id: `i${order}` });
const rowsOf = (count) => Array.from({ length: count }, (_, i) => row(i + 1));
const section = (id, before_position) => ({ id, title: id, before_position });
const orders = (rows) => rows.map((r) => r.order);

test('a section sits above the track at its 0-based position', () => {
	const { rows, headers } = sections.arrangeSections(rowsOf(4), [section('x', 2)], new Set());
	assert.deepEqual(orders(rows), [1, 2, 3, 4]);
	assert.deepEqual(
		headers.map((h) => [h.beforeIndex, h.slot, h.trackCount]),
		[[2, 2, 2]]
	);
});

test('a section past the last track renders at the end, empty', () => {
	const { headers } = sections.arrangeSections(rowsOf(2), [section('x', 2)], new Set());
	assert.deepEqual([headers[0].beforeIndex, headers[0].trackCount], [2, 0]);
});

test('collapsing hides only that section, and removes no track from its count', () => {
	const lines = [section('x', 1), section('y', 3)];
	const { rows, headers } = sections.arrangeSections(rowsOf(4), lines, new Set(['x']));
	assert.deepEqual(orders(rows), [1, 4]);
	assert.deepEqual(
		headers.map((h) => [h.beforeIndex, h.slot, h.trackCount, h.collapsed]),
		[
			[1, 1, 2, true],
			[1, 2, 1, false]
		]
	);
});

test('sorted rows stay inside their sections: no row crosses a boundary', () => {
	const sortedByBpm = [row(4), row(1), row(3), row(2)];
	const { rows, headers } = sections.arrangeSections(sortedByBpm, [section('x', 2)], new Set());
	assert.deepEqual(orders(rows), [1, 2, 4, 3]);
	assert.equal(headers[0].beforeIndex, 2);
});

test('dividers show in playlist order or within-section sort, never otherwise', () => {
	const base = { count: 1, sorted: false, sortWithinSections: false, searching: false };
	assert.equal(sections.sectionsVisible(base), true);
	assert.equal(sections.sectionsVisible({ ...base, sorted: true }), false);
	assert.equal(sections.sectionsVisible({ ...base, sorted: true, sortWithinSections: true }), true);
	assert.equal(sections.sectionsVisible({ ...base, searching: true }), false);
	assert.equal(sections.sectionsVisible({ ...base, count: 0 }), false);
});

test('dragging a section takes its hidden tracks too', () => {
	const lines = [section('x', 1), section('y', 3)];
	assert.deepEqual(sections.sectionItemIds(rowsOf(4), lines, 'x'), ['i2', 'i3']);
	assert.throws(() => sections.sectionItemIds(rowsOf(4), lines, 'nope'));
});

test('slot and row indices invert each other', () => {
	const lines = [section('x', 0), section('y', 2), section('z', 2)];
	const { rows, headers } = sections.arrangeSections(rowsOf(3), lines, new Set());
	assert.deepEqual(
		rows.map((_, i) => sections.slotOfRow(headers, i)),
		[1, 2, 5]
	);
	for (let i = 0; i < rows.length; i += 1) {
		const slot = sections.slotOfRow(headers, i);
		assert.equal(sections.rowsAboveSlot(headers, slot, rows.length), i);
	}
});

test('[if] the row window pads do not add up to every slot [then broken]', () => {
	const rowHeight = 22;
	const lines = [section('a', 0), section('b', 10), section('c', 10), section('d', 60)];
	const { rows, headers } = sections.arrangeSections(rowsOf(60), lines, new Set());
	const total = sections.slotCountOf(headers, rows.length);
	for (let start = 0; start < total; start += 1) {
		const end = Math.min(total, start + 15);
		const slots = { startIndex: start, endIndex: end, topPad: 0, bottomPad: 0 };
		const win = sections.rowWindow(slots, headers, rows.length, rowHeight);
		const trailing = win.endIndex === rows.length;
		const rendered =
			win.endIndex -
			win.startIndex +
			headers.filter(
				(h) =>
					(h.beforeIndex >= win.startIndex && h.beforeIndex < win.endIndex) ||
					(trailing && h.beforeIndex === rows.length)
			).length;
		assert.equal(
			win.topPad + win.bottomPad + rendered * rowHeight,
			total * rowHeight,
			`window from slot ${start}`
		);
	}
});

test('with no sections the window passes through untouched', () => {
	const slots = { startIndex: 3, endIndex: 9, topPad: 66, bottomPad: 44 };
	assert.equal(sections.rowWindow(slots, [], 11, 22), slots);
});

test('[if] a drop lands anywhere but directly below the gap [then broken]', () => {
	const lines = [section('x', 1), section('y', 3)];
	const { rows, headers } = sections.arrangeSections(rowsOf(4), lines, new Set());
	const display = [
		{ before_item_id: 'i1' },
		{ before_section_id: 'x' },
		{ before_item_id: 'i2' },
		{ before_item_id: 'i3' },
		{ before_section_id: 'y' },
		{ before_item_id: 'i4' },
		{}
	];
	display.forEach((expected, gap) => {
		assert.deepEqual(sections.dropTargetAt(gap, headers, rows), expected, `gap ${gap}`);
	});
});

test('a gap below a collapsed section lands after its hidden tracks', () => {
	const lines = [section('x', 1), section('y', 3)];
	const { rows, headers } = sections.arrangeSections(rowsOf(4), lines, new Set(['x']));
	assert.deepEqual(sections.dropTargetAt(2, headers, rows), { before_section_id: 'y' });
	assert.deepEqual(sections.dropTargetAt(1, headers, rows), { before_section_id: 'x' });
});

test('a row with no membership id refuses rather than guessing a target', () => {
	assert.throws(() => sections.dropTargetAt(0, [], [{ item_id: null }]));
});

test('the pointer picks the nearest gap and stays inside the list', () => {
	assert.equal(sections.gapFromPointer(-30, 22, 5), 0);
	assert.equal(sections.gapFromPointer(10, 22, 5), 0);
	assert.equal(sections.gapFromPointer(12, 22, 5), 1);
	assert.equal(sections.gapFromPointer(9000, 22, 5), 5);
});

test('rows below a collapsing section never travel more than a screen', () => {
	const slide = (trackCount) =>
		motion.sectionSlideDistance({ trackCount, rowHeight: 22, viewportHeight: 440 });
	assert.equal(slide(3), 66);
	assert.equal(slide(1000), 440);
});
