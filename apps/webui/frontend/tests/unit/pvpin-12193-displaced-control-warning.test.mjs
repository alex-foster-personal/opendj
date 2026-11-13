// PVPIN-12193 (PVPIN-11): a MIDI control left displaced after the rejoin
// arrows gets a bounded, recurring orange warning that the DJ can turn off in
// config. Pure scheduler only: no browser, no controller hardware.

import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
	DISPLACED_WARNING_KEY,
	DISPLACED_WARNING_REPEAT_MS,
	DISPLACED_WARNING_SHOW_MS,
	DisplacedControlWarning
} from '../../src/lib/rb/midi/displaced-control-warning.ts';

/** In-memory stand-in for window.localStorage. */
function memoryStorage() {
	const map = new Map();
	return {
		map,
		getItem: (key) => map.get(key) ?? null,
		setItem: (key, value) => void map.set(key, value)
	};
}

test('PVPIN-12193: a control still displaced shows the warning now and again every 30 s', () => {
	const warning = new DisplacedControlWarning(memoryStorage());
	assert.equal(DISPLACED_WARNING_SHOW_MS, 2_000);
	assert.deepEqual(warning.update(1, 1_000), { show: true, nextCheckAt: 31_000 });
	assert.deepEqual(warning.update(1, 20_000), { show: false, nextCheckAt: 31_000 });
	assert.deepEqual(warning.update(2, 31_000), {
		show: true,
		nextCheckAt: 31_000 + DISPLACED_WARNING_REPEAT_MS
	});
});

test('PVPIN-12193: a warning turned off in config never shows, also after a reload', () => {
	const storage = memoryStorage();
	const warning = new DisplacedControlWarning(storage);
	assert.equal(warning.update(1, 0).show, true);
	warning.disable();
	assert.equal(storage.map.get(DISPLACED_WARNING_KEY), 'off');
	assert.deepEqual(warning.update(1, DISPLACED_WARNING_REPEAT_MS), {
		show: false,
		nextCheckAt: null
	});
	const reloaded = new DisplacedControlWarning(storage);
	assert.equal(reloaded.enabled, false);
	assert.equal(reloaded.update(1, 0).show, false);
});

test('PVPIN-12193: a resync before the repeat interval cancels the pending repeat', () => {
	const warning = new DisplacedControlWarning(null);
	assert.equal(warning.update(1, 0).show, true);
	assert.deepEqual(warning.update(0, 10_000), { show: false, nextCheckAt: null });
	// Displaced again later: a fresh warning, not a leftover repeat.
	assert.deepEqual(warning.update(1, 12_000), { show: true, nextCheckAt: 42_000 });
});
