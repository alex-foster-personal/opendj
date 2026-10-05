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
 * - if hide-broken keeps an unchecked row then the Broken checkbox and the
 *   grey row disagree (LIBM-167): unticked Broken hides pending and absent
 * - if deck load / preview / drag call a pending row "missing on disk" then
 *   the operator goes hunting for a file that is fine -> broken
 * - if deck load / preview / drag refuse a pending row at all then a new
 *   user is told to wait for a probe they never asked for -> broken
 *   (pin c90b8036d495)
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
const PENDING_PHRASE = 'availability still checking (wait for disk probe)';
const MISSING_LOAD = 'cannot load: audio file missing on disk (broken link)';
const MISSING_PREVIEW = 'preview: audio file missing on disk (broken link)';

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

	it('hide-broken drops pending and absent rows and keeps present ones', () => {
		const rows = captured.map((w, i) => wire.rowFromPlaylistWire(w, i + 1));
		const missing = clone(captured[0]);
		missing.stable_id = 'sid-missing';
		missing.file_exists = false;
		missing.file_availability = 'absent';
		rows.push(wire.rowFromPlaylistWire(missing, rows.length + 1));
		const visible = contract.filterRows(rows, '', true);
		assert.equal(visible.length, 16);
		assert.ok(visible.every((r) => r.file_availability === 'present'));
		assert.ok(!visible.some((r) => r.stable_id === 'sid-missing'));
		assert.equal(contract.filterRows(rows, '', false).length, 41);
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

// An unchecked row is not playable. Drag refuses it the same way a deck load
// does. A known-absent file still says missing on disk.
describe('pending rows are refused; a known-absent file says missing', () => {
	it('drag refuses a pending row', () => {
		const row = wire.rowFromPlaylistWire(pendingOf(captured)[0], 1);
		assert.equal(row.file_exists, null);
		assert.match(refusal.trackDragRefusal(row), /not been confirmed/);
	});

	it('drag still calls an absent row missing (control)', () => {
		assert.match(
			refusal.trackDragRefusal({ file_exists: false, file_availability: 'absent' }),
			/missing on disk/
		);
	});

	it('drag still refuses a streaming row (control)', () => {
		assert.match(
			refusal.trackDragRefusal({ file_exists: null, is_streaming: true }),
			/streaming track/i
		);
	});
});

// REQ: PERF-RB-02
test('BrowserPanel maps through the shared module and never refuses a pending row', () => {
	const panel = read('src/lib/components/rb/BrowserPanel.svelte');
	assert.ok(!panel.includes('function _rowFromPlaylistWire('), 'a private mapper came back');
	assert.ok(!panel.includes('function _rowFromListWire('), 'a private mapper came back');
	assert.match(panel, /rowFromPlaylistWire as _rowFromPlaylistWire/);
	assert.ok(!panel.includes(PENDING_PHRASE), 'a pending row is refused with a toast again');
	// The missing-file refusal lives in the shared predicate module and fires
	// on a KNOWN-absent file only: `!row.file_exists` would call a pending row
	// (file_exists null) missing.
	const wireSrc = read('src/lib/components/rb/browser/browser-row-wire.ts');
	const at = wireSrc.indexOf(MISSING_LOAD);
	assert.ok(at > 0, MISSING_LOAD);
	const guard = wireSrc.slice(wireSrc.lastIndexOf('if (', at), at);
	assert.match(guard, /row\.file_exists === false/);
	assert.ok(!wireSrc.includes('!row.file_exists'), 'a null file_exists reads as missing');
	assert.ok(!panel.includes(MISSING_PREVIEW), 'preview toast duplicated the shared refusal');
	const support = read('src/lib/components/rb/browser/browser-panel-support.ts');
	assert.match(support, /rowFromPlaylistWire.*from '\.\/browser-row-wire'/);
});

// REQ: PERF-RB-02
test('pending rows are styled and titled as pending in both browser views', () => {
	const table = read('src/lib/components/rb/browser/TrackTable.svelte');
	assert.match(table, /class:rb-row-availability-pending=/);
	assert.match(table, /class:broken=\{rowRendersUnavailable\(row\)\}/);
	assert.match(table, /title=\{libraryRowHoverTitle\(row\)\}/);
	const column = read('src/lib/components/rb/browser/ColumnBrowser.svelte');
	assert.match(column, /class:broken=\{rowRendersUnavailable\(row\)\}/);
	assert.match(column, /title=\{libraryRowHoverTitle\(row\)\}/);
	assert.match(column, /class:pending=/);
	const wireSrc = read('src/lib/components/rb/browser/browser-row-wire.ts');
	assert.match(wireSrc, /cannot load: audio on this machine has not been confirmed/);
});

/*
 * Found driving the real shell on demon-llama (Thu 1 Oct 2026): All Tracks
 * hydrated 7,986 of 8,558 rows as AVAILABILITY_PENDING and NOTHING ever
 * settled them, so "wait for disk probe" was a wait with no end. Every
 * visible row already fetches /rb-meta, whose file_exists is a full stat
 * (an API whose type cannot say pending runs a FULL scan), so that answer
 * settles the row.
 *
 * Regression lines:
 * - if a pending row with rb-meta file_exists true stays pending then it can
 *   never be loaded or previewed -> broken
 * - if a SETTLED row is rewritten from rb-meta then the listing's typed
 *   status (awaiting_volume, streaming) is overwritten -> broken
 */
describe('a pending row settles from its rb-meta disk truth', () => {
	const pending = { file_exists: null, file_availability: 'AVAILABILITY_PENDING' };
	it('present on disk settles as present', () => {
		assert.deepEqual(
			wire.settledAvailabilityFromRbMeta(pending, { file_exists: true, is_streaming: false }),
			{ file_exists: true, file_availability: 'present' }
		);
	});
	it('missing on disk settles as absent', () => {
		assert.deepEqual(
			wire.settledAvailabilityFromRbMeta(pending, { file_exists: false, is_streaming: false }),
			{ file_exists: false, file_availability: 'absent' }
		);
	});
	it('a streaming row settles as streaming', () => {
		assert.deepEqual(
			wire.settledAvailabilityFromRbMeta(pending, { file_exists: false, is_streaming: true }),
			{ file_exists: false, file_availability: 'streaming' }
		);
	});
	it('a settled row is never rewritten (control)', () => {
		const settled = { file_exists: false, file_availability: 'awaiting_volume' };
		assert.equal(
			wire.settledAvailabilityFromRbMeta(settled, { file_exists: true, is_streaming: false }),
			null
		);
	});
	it('a pending row with no rb-meta stays pending', () => {
		assert.equal(wire.settledAvailabilityFromRbMeta(pending, null), null);
	});
});

// Regression line: a visible row's rb-meta fetch is a full stat, so its answer
// must land on the row. Deck load and preview no longer wait on it: a pending
// row loads (pin c90b8036d495), which the "never refuses" test above pins.
test('BrowserPanel settles a visible pending row from the rb-meta it fetched', () => {
	const panel = read('src/lib/components/rb/BrowserPanel.svelte');
	assert.match(
		panel,
		/row\.rb_meta = await _fetchRbMetaWithRetry\(row\.stable_id\);\s*_applySettledAvailability\(row\);/,
		'a visible row never settles from the rb-meta it already fetched'
	);
	// Control: settling goes through the pure mapper, which never rewrites a settled row.
	assert.match(panel, /function _applySettledAvailability\(row: LoadableRow\): void \{\s*const settled = settledAvailabilityFromRbMeta\(/);
});
