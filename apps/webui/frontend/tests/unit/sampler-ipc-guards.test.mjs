/**
 * Issue #10786 (S9): the edge sampler's bus commands are parsed with exact keys and exactly one
 * slot address before the performance bus runs them.
 *
 * Regression lines:
 * - if a good sampler payload is refused then the bus cannot reach the UI action
 * - if a payload with both, or neither, of slot_id and ref parses then the slot is guessed
 * - if stop all or preview stop is mixed with a slot, or set_page has both set and delta, it parses
 * - if a settings patch outside the sampler-types enums and ranges parses then the PATCH is refused late
 * - if a filter, capture deck or editor close outside its domain parses then the bus guesses
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let g;
before(async () => {
	g = await loadTypeScriptModule('src/lib/player/sampler/sampler-ipc-guards.ts');
});

const GOOD = [
	{ type: 'sampler_enabled', on: true },
	{ type: 'sampler_enabled', on: false },
	{ type: 'sampler_pin', ref: 'S2-01', pinned: true },
	{ type: 'sampler_pin', ref: 'R-03', pinned: false },
	{ type: 'sampler_pin', slot_id: 'abc', pinned: true },
	{ type: 'sampler_trigger', ref: 'S1-05' },
	{ type: 'sampler_trigger', slot_id: 'abc' },
	{ type: 'sampler_stop', ref: 'R-03' },
	{ type: 'sampler_stop', all: true },
	{ type: 'sampler_release', ref: 'S1-05' },
	{ type: 'sampler_release', slot_id: 'abc' },
	{ type: 'sampler_preview', slot_id: 'abc' },
	{ type: 'sampler_preview', stop: true },
	{ type: 'sampler_set_page', edge: 'L', set: 2 },
	{ type: 'sampler_set_page', edge: 'R', delta: -1 },
	{ type: 'sampler_search', edge: 'L', query: 'vocal chop' },
	{ type: 'sampler_search', edge: 'R', query: '' },
	{ type: 'sampler_filter', edge: 'R', filter: 'vocals', on: true },
	{ type: 'sampler_results', edge: 'L', show: true },
	{ type: 'sampler_slot_update', ref: 'S1-01', name: 'Horn' },
	{ type: 'sampler_slot_update', slot_id: 'abc', name: '', description: '' },
	{
		type: 'sampler_slot_update',
		ref: 'S1-01',
		settings: { launch: 'beat', repeat: 4, level: 80, downbeat_offset: -2 }
	},
	{ type: 'sampler_set_name', set_id: 'set-1', name: 'Warmup', description: '' },
	{ type: 'sampler_capture', deck: 2, set: 0, index: 7 },
	{ type: 'sampler_edit', ref: 'S1-05' },
	{ type: 'sampler_edit', close: true },
	{ type: 'sampler_delete', slot_id: 'cap-1' }
];

const BAD = [
	['unknown key', { type: 'sampler_enabled', on: true, extra: 1 }],
	['non-boolean on', { type: 'sampler_enabled', on: 'yes' }],
	['both addresses', { type: 'sampler_pin', ref: 'S1-05', slot_id: 'abc', pinned: true }],
	['no address', { type: 'sampler_pin', pinned: true }],
	['bad ref', { type: 'sampler_pin', ref: 'S1-5', pinned: true }],
	['zero set ref', { type: 'sampler_pin', ref: 'S0-01', pinned: true }],
	['blank slot_id', { type: 'sampler_pin', slot_id: ' ', pinned: true }],
	['no pinned', { type: 'sampler_pin', ref: 'S1-01' }],
	['trigger with no address', { type: 'sampler_trigger' }],
	['all with a slot', { type: 'sampler_stop', all: true, ref: 'S1-05' }],
	['all false', { type: 'sampler_stop', all: false }],
	['release with all', { type: 'sampler_release', all: true }],
	['stop with a slot', { type: 'sampler_preview', stop: true, slot_id: 'abc' }],
	['set and delta', { type: 'sampler_set_page', edge: 'L', set: 1, delta: 1 }],
	['neither set nor delta', { type: 'sampler_set_page', edge: 'L' }],
	['zero delta', { type: 'sampler_set_page', edge: 'L', delta: 0 }],
	['fractional set', { type: 'sampler_set_page', edge: 'L', set: 1.5 }],
	['bad edge', { type: 'sampler_set_page', edge: 'top', set: 0 }],
	['no query', { type: 'sampler_search', edge: 'L' }],
	['unknown filter', { type: 'sampler_filter', edge: 'L', filter: 'loud', on: true }],
	['non-boolean show', { type: 'sampler_results', edge: 'L', show: 1 }],
	['nothing to update', { type: 'sampler_slot_update', ref: 'S1-01' }],
	['level 101', { type: 'sampler_slot_update', ref: 'S1-01', settings: { level: 101 } }],
	['offset 4', { type: 'sampler_slot_update', ref: 'S1-01', settings: { downbeat_offset: 4 } }],
	['offset 1.5', { type: 'sampler_slot_update', ref: 'S1-01', settings: { downbeat_offset: 1.5 } }],
	['repeat 3', { type: 'sampler_slot_update', ref: 'S1-01', settings: { repeat: 3 } }],
	['unknown launch', { type: 'sampler_slot_update', ref: 'S1-01', settings: { launch: 'never' } }],
	['unknown setting', { type: 'sampler_slot_update', ref: 'S1-01', settings: { volume: 1 } }],
	['empty settings', { type: 'sampler_slot_update', ref: 'S1-01', settings: {} }],
	['blank set name', { type: 'sampler_set_name', set_id: 'set-1', name: ' ', description: '' }],
	['deck 5', { type: 'sampler_capture', deck: 5, set: 0, index: 0 }],
	['negative index', { type: 'sampler_capture', deck: 1, set: 0, index: -1 }],
	['close with a slot', { type: 'sampler_edit', close: true, ref: 'S1-05' }],
	['delete with no address', { type: 'sampler_delete' }]
];

for (const cmd of GOOD) {
	test(`a good ${cmd.type} payload parses unchanged (${JSON.stringify(cmd)})`, () => {
		/** [if] a well-formed sampler payload is refused or reshaped [then] the bus cannot reach the UI action, [else stop]. */
		assert.ok(g.isSamplerCommandType(cmd.type));
		assert.deepEqual(g.parseSamplerCommand(cmd), cmd);
	});
}

for (const [why, cmd] of BAD) {
	test(`${cmd.type} with ${why} is refused`, () => {
		/** [if] a malformed sampler payload parses [then] the guard lets a bad command through, [else stop]. */
		assert.throws(() => g.parseSamplerCommand(cmd));
	});
}

test('only sampler types are claimed by the sampler guard', () => {
	/** [if] a non-sampler or not-yet-built type is claimed [then] performance-ipc would route it wrongly, [else stop]. */
	assert.equal(g.isSamplerCommandType('set_waveform_design'), false);
	assert.equal(g.isSamplerCommandType('sampler_starter'), false);
});
