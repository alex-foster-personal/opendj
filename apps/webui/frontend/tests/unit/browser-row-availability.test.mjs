/**
 * PERF-RB-01 (issue #1037, PR #3644): a playlist whose disk truth is still
 * being probed must LOAD, and its pending rows must read as pending, never
 * as missing.
 *
 * The server made `file_exists` `boolean | null` and added
 * `file_availability`; the browser's row mapper still demanded a boolean, so
 * any playlist with more than 16 cold/stale rows (the row-hydration stat
 * budget) threw "hydrated playlist row N malformed" and failed to load at
 * all. The input here is a CAPTURED real server response (manifest beside
 * it), not a hand-built row: 40 rows, 16 stat'ed present, 24 pending.
 *
 * Requirement: PERF-RB-02.
 *
 * Regression lines:
 * - if a pending row with file_exists null throws in the mapper then a large
 *   cold playlist never loads -> broken
 * - if null is accepted on a SETTLED row, or a row without file_availability
 *   loads, then a broken backend contract is silently guessed -> broken
 * - if hide-broken hides pending rows then most of a cold playlist vanishes
 * - if deck load / preview / drag call a pending row "missing on disk" then
 *   the operator goes hunting for a file that is fine -> broken
 */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const read = (p) => readFileSync(fileURLToPath(new URL(`../../${p}`, import.meta.url)), 'utf8');
const FIXTURE = 'tests/unit/fixtures/playlist-pending-availability-captured.json';
const MANIFEST = 'tests/unit/fixtures/playlist-pending-availability-captured.manifest.json';
const PENDING_LOAD = 'cannot load: availability still checking (wait for disk probe)';
const PENDING_PREVIEW = 'preview: availability still checking (wait for disk probe)';

let wire;
let contract;
let refusal;
let captured;
before(async () => {
	wire = await loadTypeScriptModule('src/lib/components/rb/browser/browser-row-wire.ts');
	contract = await loadTypeScriptModule('src/lib/components/rb/browser/pane-contract.svelte.ts');
	refusal = await loadTypeScriptModule('src/lib/rb/track-drag-refusal.ts');
	const text = read(FIXTURE);
	const manifest = JSON.parse(read(MANIFEST));
	const entry = manifest.files['playlist-pending-availability-captured.json'];
	assert.equal(createHash('sha256').update(text).digest('hex'), entry.sha256, 'capture was edited');
	captured = JSON.parse(text);
});

const clone = (row) => JSON.parse(JSON.stringify(row));
const pendingOf = (rows) => rows.filter((r) => r.file_availability === 'AVAILABILITY_PENDING');

describe('a playlist with more than 16 pending rows loads', () => {
	it('the capture really is the over-budget case', () => {
		assert.equal(captured.length, 40);
		assert.equal(pendingOf(captured).length, 24);
		assert.ok(pendingOf(captured).every((r) => r.file_exists === null));
	});

	it('maps every captured row, pending ones as pending', () => {
		const rows = captured.map((w, i) => wire.rowFromPlaylistWire(w, i + 1));
		assert.equal(rows.length, 40);
		const pending = pendingOf(rows);
		assert.equal(pending.length, 24);
		assert.ok(pending.every((r) => r.file_exists === null));
		const present = rows.filter((r) => r.file_availability === 'present');
		assert.equal(present.length, 16);
		assert.ok(present.every((r) => r.file_exists === true));
	});

	it('the same rows load through the All Tracks list mapper', () => {
		const rows = captured.map((w, i) => wire.rowFromListWire({ ...w, notes: null }, i + 1));
		assert.equal(pendingOf(rows).length, 24);
	});

	it('hide-broken keeps pending rows and still drops a missing one', () => {
		const rows = captured.map((w, i) => wire.rowFromPlaylistWire(w, i + 1));
		const missing = clone(captured[0]);
		missing.stable_id = 'sid-missing';
		missing.file_exists = false;
		missing.file_availability = 'absent';
		rows.push(wire.rowFromPlaylistWire(missing, rows.length + 1));
		const visible = contract.filterRows(rows, '', true);
		assert.equal(visible.length, 40);
		assert.ok(!visible.some((r) => r.stable_id === 'sid-missing'));
	});
});

describe('the mapper still refuses a broken contract', () => {
	const settled = () => clone(captured.find((r) => r.file_availability === 'present'));
	const pending = () => clone(pendingOf(captured)[0]);

	it('null file_exists on a settled row throws', () => {
		const row = settled();
		row.file_exists = null;
		assert.throws(() => wire.rowFromPlaylistWire(row, 1), /malformed/);
		assert.throws(() => wire.rowFromListWire(row, 1), /non-track payload/);
	});

	it('a row without file_availability throws', () => {
		const row = settled();
		delete row.file_availability;
		assert.throws(() => wire.rowFromPlaylistWire(row, 1), /malformed/);
	});

	it('an unknown status throws', () => {
		const row = settled();
		row.file_availability = 'maybe';
		assert.throws(() => wire.rowFromPlaylistWire(row, 1), /malformed/);
	});

	it('a pending row claiming a boolean throws', () => {
		const row = pending();
		row.file_exists = false;
		assert.throws(() => wire.rowFromPlaylistWire(row, 1), /malformed/);
	});
});

describe('pending rows have their own reason, not "missing"', () => {
	it('drag refuses a pending row as pending', () => {
		const row = wire.rowFromPlaylistWire(pendingOf(captured)[0], 1);
		assert.equal(refusal.trackDragRefusal(row), PENDING_LOAD);
		assert.doesNotMatch(refusal.trackDragRefusal(row), /missing/);
	});

	it('drag still calls an absent row missing (control)', () => {
		assert.match(
			refusal.trackDragRefusal({ file_exists: false, file_availability: 'absent' }),
			/missing on disk/
		);
	});
});

test('BrowserPanel maps through the shared module and words pending refusals', () => {
	const panel = read('src/lib/components/rb/BrowserPanel.svelte');
	assert.ok(!panel.includes('function _rowFromPlaylistWire('), 'a private mapper came back');
	assert.ok(!panel.includes('function _rowFromListWire('), 'a private mapper came back');
	assert.match(panel, /rowFromPlaylistWire as _rowFromPlaylistWire/);
	assert.ok(panel.includes(PENDING_LOAD), 'deck load lost its pending reason');
	assert.ok(panel.includes(PENDING_PREVIEW), 'preview lost its pending reason');
	// Pending is checked BEFORE the missing-file branch in both paths, or a
	// null file_exists falls into "missing on disk".
	for (const [pendingPhrase, missingPhrase] of [
		[PENDING_LOAD, 'cannot load: audio file missing on disk (broken link)'],
		[PENDING_PREVIEW, 'preview: audio file missing on disk (broken link)']
	]) {
		assert.ok(panel.indexOf(pendingPhrase) < panel.indexOf(missingPhrase), pendingPhrase);
	}
	const support = read('src/lib/components/rb/browser/browser-panel-support.ts');
	assert.match(support, /rowFromPlaylistWire.*from '\.\/browser-row-wire'/);
});

test('pending rows are styled and titled as pending in both browser views', () => {
	const table = read('src/lib/components/rb/browser/TrackTable.svelte');
	assert.match(table, /class:rb-row-availability-pending=/);
	assert.match(table, /availability still checking \(wait for disk probe\)/);
	const column = read('src/lib/components/rb/browser/ColumnBrowser.svelte');
	assert.match(column, /class:broken=\{row\.file_exists === false\}/);
	assert.match(column, /class:pending=/);
});
