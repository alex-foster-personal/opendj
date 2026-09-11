/**
 * Fixed-index auto-cue proposal assignment for empty hot-cue slots A-H.
 *
 * Regression lines:
 * - if filling slot A slides proposals[0] onto B then assignment is wrong
 * - if a kind outside intro/drop/break/outro/'' still renders then fail-closed is broken
 * - if more than 8 proposals occupy a slot then the router cap leaked into the UI
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

function cue(overrides) {
	return {
		time_s: 0,
		kind: 'intro',
		confidence: 0.8,
		source: 'heuristic',
		rms_dbfs: -10,
		...overrides
	};
}

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/auto-cue-proposals.ts');
});

test('HOT_CUE_SLOTS is A through H in order', () => {
	assert.deepEqual(mod.HOT_CUE_SLOTS, ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H']);
});

test('all eight empty slots map index i to slot letter i', () => {
	const proposals = [
		cue({ kind: 'intro', time_s: 1 }),
		cue({ kind: 'drop', time_s: 2 }),
		cue({ kind: 'break', time_s: 3 }),
		cue({ kind: 'outro', time_s: 4 }),
		cue({ kind: '', time_s: 5 }),
		cue({ kind: 'intro', time_s: 6 }),
		cue({ kind: 'drop', time_s: 7 }),
		cue({ kind: 'break', time_s: 8 })
	];
	const filled = new Set();
	for (let i = 0; i < 8; i += 1) {
		const slot = mod.HOT_CUE_SLOTS[i];
		assert.equal(mod.visibleProposalForSlot(slot, filled, proposals), proposals[i]);
	}
});

test('a filled slot A hides proposals[0] without sliding it onto B', () => {
	const proposals = [cue({ kind: 'intro', time_s: 8 }), cue({ kind: 'drop', time_s: 32 })];
	const filled = new Set(['A']);
	assert.equal(mod.visibleProposalForSlot('A', filled, proposals), null);
	assert.equal(mod.visibleProposalForSlot('B', filled, proposals), proposals[1]);
	assert.equal(mod.visibleProposalForSlot('C', filled, proposals), null);
});

test('fewer proposals than slots leave later letters empty', () => {
	const proposals = [cue({ kind: 'intro', time_s: 4 })];
	const filled = new Set();
	assert.equal(mod.visibleProposalForSlot('A', filled, proposals), proposals[0]);
	assert.equal(mod.visibleProposalForSlot('B', filled, proposals), null);
	assert.equal(mod.visibleProposalForSlot('H', filled, proposals), null);
});

test('proposals past index 7 are ignored', () => {
	const proposals = Array.from({ length: 9 }, (_, i) => cue({ kind: 'intro', time_s: i }));
	const filled = new Set();
	assert.equal(mod.visibleProposalForSlot('H', filled, proposals), proposals[7]);
	assert.equal(
		mod.visibleProposalForSlot('H', filled, proposals).time_s,
		7,
		'the ninth proposal must not occupy slot H'
	);
});

test('a kind outside the allowed set is not shown', () => {
	const proposals = [cue({ kind: 'verse', time_s: 10 })];
	assert.equal(mod.visibleProposalForSlot('A', new Set(), proposals), null);
});

test('caption and title strings are pinned', () => {
	assert.equal(mod.proposalCaption('intro'), 'proposed intro');
	assert.equal(mod.proposalCaption('drop'), 'proposed drop');
	assert.equal(mod.proposalCaption('break'), 'proposed break');
	assert.equal(mod.proposalCaption('outro'), 'proposed outro');
	assert.equal(mod.proposalCaption(''), 'proposed');
	assert.equal(mod.proposalCaption('verse'), 'proposed');

	assert.equal(mod.formatProposalTime(12.9), '0:12');
	assert.equal(mod.formatProposalTime(65), '1:05');
	assert.equal(mod.formatProposalTime(605.2), '10:05');

	assert.equal(mod.proposalTitle('intro', 12.4), 'proposed intro at 0:12 - not saved');
	assert.equal(mod.proposalTitle('', 12.4), 'proposed cue at 0:12 - not saved');
});
