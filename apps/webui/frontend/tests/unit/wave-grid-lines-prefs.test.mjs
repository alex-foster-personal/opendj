// requirement: WAVEQS-11736
// [if] the grid pref defaults off beats_bars, the setter skips the disk patch, or an unknown value is stored [then] this fails, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let grid;
before(async () => {
	grid = await loadTypeScriptModule('src/lib/rb/wave-grid-lines.ts');
});

test('WAVEQS-11736 the default is beats_bars', () => {
	assert.deepEqual(grid.WAVE_GRID_LINES_PREF_DEFAULTS, { wave_grid_lines: 'beats_bars' });
	assert.deepEqual([...grid.WAVE_GRID_LINES_VALUES], ['beats_bars', 'bars', 'off']);
});

test('WAVEQS-11736 the setter writes state, persists and sends the disk patch', () => {
	const state = { wave_grid_lines: 'beats_bars' };
	const patches = [];
	let persisted = 0;
	const { setWaveGridLines } = grid.makeWaveGridLinesSetter(
		state,
		() => (persisted += 1),
		(p) => patches.push(p)
	);
	setWaveGridLines('bars');
	assert.equal(state.wave_grid_lines, 'bars');
	assert.equal(persisted, 1);
	assert.deepEqual(patches, [{ wave_grid_lines: 'bars' }]);
	assert.throws(() => setWaveGridLines('dots'), /beats_bars\|bars\|off/);
	assert.equal(state.wave_grid_lines, 'bars', 'a refused value leaves state alone');
	assert.equal(persisted, 1);
});

test('WAVEQS-11736 a malformed stored value throws naming the storage key; absent keeps the default', () => {
	assert.deepEqual(grid.validateWaveGridLinesPrefFields({}, 'k'), {});
	assert.deepEqual(grid.validateWaveGridLinesPrefFields({ wave_grid_lines: 'off' }, 'k'), {
		wave_grid_lines: 'off'
	});
	assert.throws(
		() => grid.validateWaveGridLinesPrefFields({ wave_grid_lines: 'dots' }, 'mdt.key'),
		/mdt\.key.*clear the localStorage key/
	);
});

test('WAVEQS-11736 Settings apply and disk hydrate both carry the key', () => {
	assert.match(readFileSync('src/lib/settings/apply.ts', 'utf8'), /^\t'wave_grid_lines',$/m);
	assert.match(
		readFileSync('src/lib/rb/prefs-hydrate.ts', 'utf8'),
		/uiPrefs\.wave_grid_lines = waveGridLines/
	);
});
