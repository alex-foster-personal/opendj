import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// DECKUX-LABEL-01: the strip names the grid provider in a small neutral chip.
let chip;
before(async () => {
	chip = await loadTypeScriptModule('src/lib/components/rb/deck/strip-grid-provider.ts');
});

const BEATS = [
	{ n: 1, bpm: 125, t: 0.1 },
	{ n: 2, bpm: 125, t: 0.58 }
];
const anlz = (beatgrid, extra = {}) => ({
	beatgrid,
	beatgrid_source: beatgrid.source,
	beatgrid_own_unavailable_reason: null,
	...extra
});

test('[if] no analysis payload is loaded [then] gridProviderChip is null, [else stop].', () => {
	assert.equal(chip.gridProviderChip(null), null);
});

test('[if] an unmapped track serves its own grid [then] the chip reads "Grid: own · Wave: own", [else stop].', () => {
	const got = chip.gridProviderChip(
		anlz({ source: 'own', status: 'ok', beats: BEATS }, { beatgrid_source_basis: 'unmapped-default' })
	);
	assert.equal(got.text, 'Grid: own · Wave: own');
	assert.match(got.title, /rekordbox has no analysis/);
});

test('[if] a rekordbox-mapped track serves the rekordbox grid [then] the chip reads "Grid: rekordbox" with no guessed wave source, [else stop].', () => {
	const got = chip.gridProviderChip(anlz({ source: 'rekordbox', beats: BEATS }, { beatgrid_source_basis: 'selection' }));
	assert.equal(got.text, 'Grid: rekordbox');
});

test('[if] own is selected on a mapped track [then] the chip reads "Grid: own" only, [else stop].', () => {
	const got = chip.gridProviderChip(anlz({ source: 'own', status: 'ok', beats: BEATS }, { beatgrid_source_basis: 'selection' }));
	assert.equal(got.text, 'Grid: own');
});

test('[if] own is selected but not yet analysed [then] the chip reads "Grid: own pending" and carries the reason, [else stop].', () => {
	const got = chip.gridProviderChip(
		anlz({ source: 'own', status: 'missing', beats: [] }, { beatgrid_own_unavailable_reason: 'own analysis not run yet' })
	);
	assert.equal(got.text, 'Grid: own pending');
	assert.match(got.title, /own analysis not run yet/);
});

test('[if] the grid failed or is empty [then] the chip reads "Grid: none" and says Beat Sync is off, [else stop].', () => {
	const failed = chip.gridProviderChip(anlz({ source: 'own', status: 'failed', reason: 'no onsets', beats: [] }));
	assert.equal(failed.text, 'Grid: none');
	assert.match(failed.title, /^no onsets\. .*Beat Sync/);
	assert.equal(chip.gridProviderChip(anlz({ source: 'rekordbox', beats: [] })).text, 'Grid: none');
});
