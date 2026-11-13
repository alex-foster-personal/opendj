// requirement: DECKUX-9981
// [if] cueListRows does not sort memory cues, hot cues and loops by in_ms with kind, time, comment and colour [then] broken
// [if] a loop's beat length comes from vendor beat_loop_size instead of the PQTZ grid [then] broken
// [if] a row click does not build an exact seek, or an unquantized loop for a loop row [then] broken
// [if] the engaged-loop row and the row nearest the playhead are not picked out [then] broken
// [if] the tab is not inert with a reason on an empty deck, or the last tab is not kept per session [then] broken
// [if] PadModeMenu does not render the Cues tab from cue-list.ts, or Deck.svelte does not pass the deck and hot-cue clear path [then] broken
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const read = (rel) => readFileSync(`${SRC}/${rel}`, 'utf8');

let m;

before(async () => {
	m = await loadTypeScriptModule('src/lib/rb/cue-list.ts');
});

// A 120 BPM grid: one beat every 0.5 s from 0 to 60 s.
const beats = Array.from({ length: 121 }, (_, i) => ({ t: i * 0.5 }));

const cue = (over) => ({
	kind: 'memory',
	slot: null,
	in_ms: 0,
	out_ms: null,
	is_loop: false,
	active_loop: false,
	beat_loop_size: null,
	color_table_index: null,
	comment: null,
	...over
});

describe('cueListRows', () => {
	test('sorts every stored cue by in_ms with kind, time, comment and colour', () => {
		/** Rows come back in time order whatever the input order. */
		const rows = m.cueListRows(
			[
				cue({ kind: 'hot_cue', slot: 'B', in_ms: 32000, comment: 'drop', color_table_index: 5 }),
				cue({ kind: 'memory', in_ms: 1500 }),
				cue({ kind: 'loop', in_ms: 8000, out_ms: 16000, is_loop: true }),
				cue({ kind: 'transition_in', in_ms: 500 })
			],
			beats
		);
		assert.deepEqual(
			rows.map((r) => [r.kind, r.time, r.comment, r.color_table_index]),
			[
				['memory', '00:01.500', null, null],
				['loop', '00:08.000', null, null],
				['hot_cue', '00:32.000', 'drop', 5]
			]
		);
	});

	test('loop beat length comes from the grid, never beat_loop_size', () => {
		/** A packed vendor size must not leak into the label. */
		const [row] = m.cueListRows(
			[cue({ kind: 'loop', in_ms: 8000, out_ms: 16000, is_loop: true, beat_loop_size: 1048577 })],
			beats
		);
		assert.equal(row.loopBeats, 16);
		const [offGrid] = m.cueListRows(
			[cue({ kind: 'loop', in_ms: 8000, out_ms: 16000, is_loop: true, beat_loop_size: 16 })],
			[]
		);
		assert.equal(offGrid.loopBeats, null);
	});

	test('hot cues come from the live bank, memory cues and loops from ANLZ', () => {
		/** A stale ANLZ hot cue is replaced by the bank's current one. */
		const sources = m.deckCueListSources(
			[cue({ kind: 'hot_cue', slot: 'A', in_ms: 1000 }), cue({ kind: 'memory', in_ms: 2000 })],
			[
				{
					slot: 'A',
					in_ms: 4000,
					out_ms: null,
					is_loop: false,
					beat_loop_size: null,
					color_table_index: 1,
					comment: 'x'
				}
			]
		);
		assert.deepEqual(
			m.cueListRows(sources, beats).map((r) => [r.kind, r.in_ms]),
			[
				['memory', 2000],
				['hot_cue', 4000]
			]
		);
	});
});

describe('row actions and highlights', () => {
	test('a point row seeks exactly and a loop row engages its loop unquantized', () => {
		/** Click builds the performance command the agent path also accepts. */
		const rows = m.cueListRows(
			[
				cue({ in_ms: 1500 }),
				cue({ kind: 'hot_cue', slot: 'C', in_ms: 8000, out_ms: 10000, is_loop: true })
			],
			beats
		);
		assert.deepEqual(m.cueRowCommand(1, rows[0]), {
			type: 'seek',
			deck: 1,
			position_ms: 1500,
			exact: true
		});
		assert.deepEqual(m.cueRowCommand(1, rows[1]), {
			type: 'loop',
			deck: 1,
			loop: { in_ms: 8000, out_ms: 10000 },
			quantize: false
		});
	});

	test('the engaged loop row and the row nearest the playhead are picked out', () => {
		/** Highlights follow the deck's loop and position. */
		const rows = m.cueListRows(
			[
				cue({ in_ms: 1000 }),
				cue({ kind: 'loop', in_ms: 8000, out_ms: 16000, is_loop: true }),
				cue({ in_ms: 30000 })
			],
			beats
		);
		assert.deepEqual(
			m.cueListHighlights(rows, 9000, { in_ms: 8000, out_ms: 16000, engaged: true }),
			{
				engagedIndex: 1,
				nearestIndex: 1
			}
		);
		assert.deepEqual(
			m.cueListHighlights(rows, 25000, { in_ms: 8000, out_ms: 16000, engaged: false }),
			{
				engagedIndex: -1,
				nearestIndex: 2
			}
		);
		assert.deepEqual(m.cueListHighlights([], 0, null), { engagedIndex: -1, nearestIndex: -1 });
	});

	test('inert with a reason on an empty deck; last tab kept per deck for the session', () => {
		/** No track means a stated reason, and the tab choice survives a reopen. */
		assert.match(m.cueListInertReason(null), /No track loaded/);
		assert.equal(m.cueListInertReason('sid'), null);
		assert.equal(m.lastPadMenuTab(2), 'modes');
		m.rememberPadMenuTab(2, 'cues');
		assert.equal(m.lastPadMenuTab(2), 'cues');
		assert.equal(m.lastPadMenuTab(1), 'modes');
	});
});

test('PadModeMenu renders the Cues tab from the live deck and Deck passes the clear path', () => {
	/** Source wiring: the dropdown uses the pure helper and the bank's clear with undo. */
	const menu = read('lib/components/rb/deck/PadModeMenu.svelte');
	assert.match(menu, /from '\$lib\/rb\/cue-list'/);
	assert.match(menu, /const deck = \$derived\(getDeckState\(deckId\)\)/);
	assert.match(menu, /cueRowCommand\(deckId, row\)/);
	assert.match(menu, /onRestoreHotCue\(slot, mutation\.revision, reversal\.reversal_id\)/);
	const deck = read('lib/components/rb/Deck.svelte');
	assert.match(
		deck,
		/<PadModeMenu \{deckId\} stacked onDeleteHotCue=\{hotCueActions\.clearHotCueAt\}/
	);
});
