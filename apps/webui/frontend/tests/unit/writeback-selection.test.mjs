import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let selection;

before(async () => {
	selection = await loadTypeScriptModule('src/lib/rb/writeback-selection.ts');
});

const rekordbox = {
	vendor: 'rekordbox',
	target_mode: 'live',
	target_path: '/library/rekordbox/master.db',
	target_id: 'rb-1'
};

const djay = {
	vendor: 'djay',
	target_mode: 'live',
	target_path: '/library/djay/MediaLibrary.db',
	target_id: 'dj-7'
};

test('a slower prior target or plan request cannot publish after selection changes', () => {
	const gate = new selection.WritebackRequestGate();
	const priorRequest = gate.capture(rekordbox);
	const currentRequest = gate.capture(djay);

	assert.equal(gate.isCurrent(priorRequest, djay), false);
	assert.equal(gate.isCurrent(currentRequest, djay), true);
});

test('apply and rollback refuse a plan or result bound to another native target', () => {
	assert.equal(selection.planMatchesSelection(rekordbox, rekordbox), true);
	assert.equal(selection.planMatchesSelection(rekordbox, djay), false);
	assert.equal(selection.resultMatchesSelection(rekordbox, rekordbox), true);
	assert.equal(selection.resultMatchesSelection(rekordbox, djay), false);
	assert.equal(selection.canRollbackWriteback(false, rekordbox, rekordbox, rekordbox), false);
	assert.equal(selection.canRollbackWriteback(true, rekordbox, rekordbox, rekordbox), true);
	assert.equal(selection.canRollbackWriteback(true, rekordbox, rekordbox, djay), false);
});

test('an order-only plan is disclosed as a destructive reorder, not a zero-change no-op', () => {
	assert.equal(
		selection.writebackPlanMutation({ ordered_match: false, added: [], removed: [] }),
		'reorder'
	);
	assert.equal(
		selection.writebackPlanMutation({ ordered_match: false, added: ['new'], removed: [] }),
		'membership'
	);
});
