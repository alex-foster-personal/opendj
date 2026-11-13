/**
 * WAVEPLAN-7896e (#12523): the chip text and hover title a pending plan puts on each waveform row.
 *
 * [if] an outgoing deck has a placeable plan [then ⛔] its row must read `PLAN OUT` and explain the lines.
 * [if] an incoming deck moved from its planned cue [then ⛔] its row must say how many bars it moved and from where.
 * [if] overlay placement is unavailable [then ⛔] the row must read `PLAN no curves` and say why.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const { planChipText, planChipTitle } = await loadTypeScriptModule(
	'src/lib/rb/plan-overlay-chip.ts'
);

/** A deck overlay of the given role, placeable unless `unavailable` is set. */
function deck(role, unavailable = null) {
	return { deck: role === 'clock' ? 1 : 2, role, span: null, curves: [], unavailable };
}

const MOVED = { planned_bar: 9, live_bar: 12, moved_bars: 3 };

/** An outgoing deck with a placeable plan reads PLAN OUT and its title explains the lines. */
test('outgoing deck shows PLAN OUT with the line legend', () => {
	assert.equal(planChipText(deck('clock'), null), 'PLAN OUT');
	assert.equal(planChipText(deck('clock'), MOVED), 'PLAN OUT', 'a move is the incoming deck only');
	const title = planChipTitle(deck('clock'), MOVED);
	assert.match(title, /^Transition plan pending\. /);
	assert.match(title, /Solid line: fader\. Dashed: EQ\. Dotted: filter\./);
});

/** An incoming deck on its planned cue reads PLAN IN with the plain pending title. */
test('incoming deck on its cue shows PLAN IN', () => {
	assert.equal(planChipText(deck('incoming'), null), 'PLAN IN');
	assert.match(planChipTitle(deck('incoming'), null), /^Transition plan pending\. Solid line/);
});

/** An incoming deck moved off its cue names the signed bar distance in text and title. */
test('moved incoming deck shows the signed bars it moved', () => {
	assert.equal(planChipText(deck('incoming'), MOVED), 'PLAN IN moved +3.0 bars');
	const back = { planned_bar: 9, live_bar: 7, moved_bars: -1.5 };
	assert.equal(planChipText(deck('incoming'), back), 'PLAN IN moved -1.5 bars');
	const title = planChipTitle(deck('incoming'), MOVED);
	assert.match(title, /sits \+3\.0 bars from bar 9, the cue the plan was made for \(now bar 12\)/);
	assert.match(title, /plan again to fit the new cue/);
	assert.match(title, /Solid line: fader/);
});

/** A deck whose overlay cannot be placed reads PLAN no curves and its title says why. */
test('unavailable placement shows PLAN no curves with the reason', () => {
	for (const role of ['clock', 'incoming']) {
		assert.equal(planChipText(deck(role, 'no_grid'), MOVED), 'PLAN no curves');
	}
	assert.equal(
		planChipTitle(deck('clock', 'no_grid'), null),
		'Transition plan pending, but this track has no beatgrid to place the curves on.'
	);
	assert.match(planChipTitle(deck('incoming', 'no_tempo'), MOVED), /reports no tempo/);
	assert.match(planChipTitle(deck('clock', 'track_changed'), null), /no longer holds the track/);
	assert.match(planChipTitle(deck('clock', 'build_failed'), null), /could not be built/);
	assert.equal(
		planChipTitle(deck('clock', 'mystery'), null),
		'Transition plan pending, but mystery.',
		'an unknown reason code still shows rather than vanishing'
	);
});
