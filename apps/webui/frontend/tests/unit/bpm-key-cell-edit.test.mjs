// requirement: LIBPG-7694
/**
 * Pin a720a02ecf92: double-click or right-click > Edit on a tempo or key cell
 * opens a small editor above that cell. Tempo has a Tap button that measures
 * BPM from the gaps between taps.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

function readSrc(relPath) {
	return readFileSync(`${FRONTEND_ROOT}/${relPath}`, 'utf8');
}

let edit;

before(async () => {
	edit = await loadTypeScriptModule('src/lib/components/rb/browser/bpm-key-cell-edit.ts');
});

describe('tap tempo', () => {
	test('one tap is not a tempo, two taps 500ms apart are 120 BPM', () => {
		const first = edit.tapTempoAt(edit.emptyTapTempo(), 0);
		assert.equal(first.bpm, null);
		const second = edit.tapTempoAt(first.state, 500);
		assert.equal(second.bpm, 120);
	});

	test('a third tap on the same beat keeps the mean', () => {
		let state = edit.emptyTapTempo();
		for (const t of [0, 500, 1000]) state = edit.tapTempoAt(state, t).state;
		assert.equal(edit.tapTempoAt(state, 1500).bpm, 120);
	});

	test('a gap longer than two seconds starts a new run', () => {
		const first = edit.tapTempoAt(edit.emptyTapTempo(), 0);
		const reset = edit.tapTempoAt(first.state, 2500);
		assert.equal(reset.bpm, null);
		assert.deepEqual(reset.state.timesMs, [2500]);
	});

	test('a backwards clock starts a new run', () => {
		const first = edit.tapTempoAt(edit.emptyTapTempo(), 1000);
		const reset = edit.tapTempoAt(first.state, 400);
		assert.equal(reset.bpm, null);
		assert.deepEqual(reset.state.timesMs, [400]);
	});
});

describe('key and cell text', () => {
	test('Camelot and note names become one label', () => {
		assert.equal(edit.canonicalKeyLabel('8a'), '8A');
		assert.equal(edit.canonicalKeyLabel('Gm'), '6A');
		assert.equal(edit.canonicalKeyLabel('nope'), null);
	});

	test('the nudge is the shortest chromatic step, and a same root is zero', () => {
		assert.equal(edit.semitoneNudgeBetween('8A', '8A'), 0);
		assert.equal(edit.semitoneNudgeBetween('Gm', '6A'), 0);
		assert.equal(edit.semitoneNudgeBetween('8A', '9A'), -5);
		assert.equal(edit.semitoneNudgeBetween(null, '8A'), null);
	});

	test('the editor drafts a tempo with two decimals', () => {
		assert.equal(edit.formatDraftBpm(120), '120.00');
		assert.equal(edit.formatDraftBpm(128.456), '128.46');
	});
});

test('the tempo and key cells open the editor above themselves, and tempo can be tapped', () => {
	const table = readSrc('src/lib/components/rb/browser/TrackTable.svelte');
	assert.match(table, /ondblclick=\{\(e\) => openCellEdit\(e, row, 'bpm'\)\}/);
	assert.match(table, /ondblclick=\{\(e\) => openCellEdit\(e, row, 'key'\)\}/);
	assert.match(table, /oncontextmenu=\{\(e\) => openCellEditMenu\(e, row, 'bpm'\)\}/);
	assert.match(table, /oncontextmenu=\{\(e\) => openCellEditMenu\(e, row, 'key'\)\}/);
	assert.match(table, /label: 'Edit'/);
	const pop = readSrc('src/lib/components/rb/browser/BpmKeyEditPopover.svelte');
	assert.match(pop, /data-testid="bpm-key-edit-tap"/);
	assert.match(pop, /preferred: 'above'/);
	assert.match(pop, />\s*Tap\s*</);
});

test('LIBPG-7694 the table opens the editor from the K/B cells and prints the session edit', async () => {
	const { readFileSync } = await import('node:fs');
	const table = readFileSync(new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url), 'utf8');
	assert.match(table, /ondblclick=\{\(e\) => openCellEdit\(e, row, 'key'\)\}/);
	assert.match(table, /ondblclick=\{\(e\) => openCellEdit\(e, row, 'bpm'\)\}/);
	assert.match(table, /oncontextmenu=\{\(e\) => openCellEditMenu\(e, row, 'bpm'\)\}/);
	assert.match(table, /keyCellSpans\(cellKey, uiPrefs\.key_notation, findQuery\)/);
	assert.match(table, /\{#if bpmEdited\}\s*\{bpmCellText\(\{ bpm: cellBpm, bpm_status: 'ok' \}\)\.text\}/);
	// The K/B fit measures what the cells print, edits included.
	assert.match(table, /rows: rows\.map\(\(row\) => editedMusicalRow\(row\)\)/);
	const popover = readFileSync(new URL('../../src/lib/components/rb/browser/BpmKeyEditPopover.svelte', import.meta.url), 'utf8');
	assert.match(popover, /data-testid="bpm-key-edit"/);
	assert.match(popover, /data-testid="bpm-key-edit-tap"/);
	assert.match(popover, /title=\{kind === 'bpm'/);
});

describe('cell commits stop at the first refused command (LIBPG-7694)', () => {
	let entry;
	before(async () => {
		entry = await loadTypeScriptModule('tests/unit/fixtures/bpm-key-overrides-entry.ts', {
			alias: { '$lib/rb/performance-ipc.svelte': `${FRONTEND_ROOT}/tests/unit/fixtures/bpm-key-overrides-stub-ipc.ts` }
		});
	});

	const decks = (key) => ({
		ids: [1],
		states: { 1: { stable_id: 'trk', position_ms: 0, bpm: 120, key, key_shift_semitones: 0, anlz: null } },
		pitchRanges: { 1: 8 }
	});

	test('a key edit three semitones away sends three nudges when each lands', async () => {
		entry.answerWith([]);
		// 8A (A minor) to 5A (C minor): C is three semitones above A.
		await entry.commitKeyEdit('trk', '5A', decks('8A'));
		assert.equal(entry.sent.filter((c) => c.type === 'key_nudge').length, 3);
	});

	test('a refused nudge stops the rest, and the cell keeps the typed key', async () => {
		entry.answerWith([true, false, true]);
		await entry.commitKeyEdit('trk2', '5A', { ...decks('8A'), states: { 1: { ...decks('8A').states[1], stable_id: 'trk2' } } });
		assert.equal(entry.sent.length, 2, 'no nudge after the refused one');
		assert.equal(entry.editedKey('trk2', null), '5A');
	});

	test('every nudge is one semitone, and a step past the shift range is not sent', async () => {
		entry.answerWith([]);
		await entry.commitKeyEdit('trk3', '5A', { ...decks('8A'), states: { 1: { ...decks('8A').states[1], stable_id: 'trk3' } } });
		assert.deepEqual(entry.sent.map((c) => c.semitones), [1, 1, 1]);
		entry.answerWith([]);
		const far = { ...decks('8A'), states: { 1: { ...decks('8A').states[1], stable_id: 'trk4', key_shift_semitones: 10 } } };
		await entry.commitKeyEdit('trk4', '5A', far);
		assert.equal(entry.sent.length, 0);
	});
});
