import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * An operator report, Sat 24 Oct 2026: "many tracks show alt in the BPM column". A row whose
 * bpm_status is available-not-selected (rekordbox grid selected, own analysis
 * also present) carries rekordbox's BPM, and the cell printed "alt" instead
 * (1,023 of 8,174 rows on that library).
 *
 * [if] an available-not-selected row prints anything but its BPM [then] broken.
 * [if] a row with no BPM (null, or rekordbox's unanalysed 0) prints a number or
 *   "alt" [then] broken.
 * [if] the BPM cell template carries a literal "alt" [then] broken.
 */

let cell;

before(async () => {
	cell = await loadTypeScriptModule('src/lib/rb/bpm-cell-text.ts');
});

it('available-not-selected shows the served BPM, never "alt"', () => {
	const out = cell.bpmCellText({ bpm: 126.0, bpm_status: 'available-not-selected' });
	assert.deepEqual(out, { text: '126.0', kind: 'value' });
});

it('an ok row keeps one decimal of BPM, never rounding it away', () => {
	assert.deepEqual(cell.bpmCellText({ bpm: 123.6, bpm_status: 'ok' }), { text: '123.6', kind: 'value' });
	assert.deepEqual(cell.bpmCellText({ bpm: 174.04, bpm_status: 'ok' }), { text: '174.0', kind: 'value' });
});

it('a row with no BPM shows the placeholder, whatever its status', () => {
	for (const status of ['ok', 'available-not-selected']) {
		for (const bpm of [null, 0, -1, Number.NaN]) {
			assert.deepEqual(
				cell.bpmCellText({ bpm, bpm_status: status }),
				{ text: cell.MISSING_BPM_TEXT, kind: 'missing' },
				`${status} ${bpm}`
			);
		}
	}
	assert.equal(cell.MISSING_BPM_TEXT, '--');
});

it('an absent (compacted ok) status shows the BPM when there is one', () => {
	assert.deepEqual(cell.bpmCellText({ bpm: 128, bpm_status: undefined }), { text: '128.0', kind: 'value' });
	assert.deepEqual(cell.bpmCellText({ bpm: null, bpm_status: undefined }), {
		text: cell.MISSING_BPM_TEXT,
		kind: 'missing'
	});
});

it('failed and missing statuses keep their words', () => {
	assert.equal(cell.bpmCellText({ bpm: null, bpm_status: 'failed' }).text, 'failed');
	assert.equal(cell.bpmCellText({ bpm: null, bpm_status: 'missing' }).text, 'missing');
});

it('the BPM cell template prints bpmCellText and no literal "alt"', () => {
	const table = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)),
		'utf8'
	);
	const start = table.indexOf('class="c-bpm"');
	assert.notEqual(start, -1, 'no BPM cell in TrackTable');
	const td = table.slice(table.lastIndexOf('<td', start), table.indexOf('</td>', start));
	assert.doesNotMatch(td, />\s*alt\s*</, 'BPM cell prints the word alt');
	assert.match(td, /bpmCellText\(row\)\s*\.text/, 'BPM cell does not render through bpmCellText');
});

it('the BPM hover never claims an exact BPM the cell calls missing (review P2, PR #7483)', async () => {
	const titles = await loadTypeScriptModule('src/lib/rb/bpm-cell-title.ts');
	for (const bpm of [0, -1, Number.NaN]) {
		const title = titles.bpmCellTitle({ bpm, bpm_status: 'ok', bpm_confidence: null }, null);
		assert.doesNotMatch(title, /Exact BPM/, String(bpm));
	}
	assert.match(titles.bpmCellTitle({ bpm: 123.6, bpm_status: 'ok', bpm_confidence: null }, null), /Exact BPM: 123\.6/);
	const table = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)),
		'utf8'
	);
	assert.match(table, /hasUsableBpm\(row\.bpm\) \? ` Exact BPM:/, 'TrackTable hover checks only for null');
});

// KEY-FALLBACK-01: if a Mixed In Key BPM is hidden because our own lane failed or has not run then broken
it('bpmCellText: a fallback BPM shows even when our own lane failed or is missing', () => {
	assert.deepEqual(cell.bpmCellText({ bpm: 124.0, bpm_status: 'failed' }), { text: '124.0', kind: 'value' });
	assert.deepEqual(cell.bpmCellText({ bpm: 95.5, bpm_status: 'missing' }), { text: '95.5', kind: 'value' });
	assert.equal(cell.bpmCellText({ bpm: null, bpm_status: 'missing' }).kind, 'not-analyzed');
});
