/**
 * A refused drag says why, in the same words the double-click says.
 *
 * Pin 8ba0b15d975b: "can't click and drag from library -> deck. unit test
 * should catch this." Pin 72be3e505510, one minute later, pasted the toast
 * from double-clicking the same row: "streaming track - deck load not
 * implemented (see PARITY-TODO)".
 *
 * So the drag was not broken - it was REFUSED, silently, for a row that cannot
 * load, while the other path explained itself. A silent refusal on a direct
 * manipulation gesture is indistinguishable from a broken feature, which is
 * exactly how it got reported.
 *
 * Regression lines:
 * - if a loadable row is refused then drag-to-deck is broken for real
 * - if a streaming or broken-link row is silently refused then the gesture
 *   looks broken again instead of explaining itself
 * - if the drag and load paths word the same refusal differently then the same
 *   row gives two different answers depending on how you touched it
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it, test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let trackDragRefusal;
before(async () => {
	({ trackDragRefusal } = await loadTypeScriptModule('src/lib/rb/track-drag-refusal.ts'));
});

describe('trackDragRefusal', () => {
	it('allows a row whose file is on disk', () => {
		assert.equal(trackDragRefusal({ file_exists: true }), null);
		assert.equal(trackDragRefusal({ file_exists: true, is_streaming: false }), null);
		// The row model types this `boolean | null`; null means not known to be
		// streaming, and must not read as a third state that blocks the drag.
		assert.equal(trackDragRefusal({ file_exists: true, is_streaming: null }), null);
	});

	it('refuses a streaming row, and says so', () => {
		const why = trackDragRefusal({ file_exists: true, is_streaming: true });
		assert.match(why, /streaming/i);
	});

	it('refuses a broken link, and says so', () => {
		const why = trackDragRefusal({ file_exists: false });
		assert.match(why, /missing on disk|broken link/i);
	});

	it('prefers the streaming reason over the missing-file one', () => {
		// A streaming URI has no local file by definition; reporting it as a
		// broken link would send the reader looking for a file that never was.
		assert.match(trackDragRefusal({ file_exists: false, is_streaming: true }), /streaming/i);
	});
});

test('72be3e505510 streaming refusal matches double-click wording', () => {
	const phrase = "Streaming track: Open DJ can't play streaming services";
	assert.equal(trackDragRefusal({ file_exists: true, is_streaming: true }), phrase);
	assert.equal(
		trackDragRefusal({
			file_exists: false,
			is_streaming: true,
			file_path: 'tidal:tracks:99560085'
		}),
		"Tidal streaming track: Open DJ can't play streaming services"
	);
});

test('the drag path words its refusals exactly as the load path does', () => {
	// Both paths call libraryAudioLoadRefusal. The sentences live once, in
	// browser-row-wire / streaming-availability, so deck-track-drop does not
	// copy them.
	const read = (p) => readFileSync(fileURLToPath(new URL(`../../${p}`, import.meta.url)), 'utf8');
	const refusal = read('src/lib/rb/track-drag-refusal.ts');
	const panel = read('src/lib/components/rb/BrowserPanel.svelte');
	const wire = read('src/lib/components/rb/browser/browser-row-wire.ts');
	const named = read('src/lib/components/rb/browser/streaming-availability.ts');
	assert.match(refusal, /libraryAudioLoadRefusal/);
	assert.match(panel, /libraryAudioLoadRefusal/);
	assert.match(wire, /cannot load: audio file missing on disk \(broken link\)/);
	assert.match(named, /Open DJ can't play streaming services/);
});

const TABLE_PATH = fileURLToPath(
	new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)
);
const PANEL_PATH = fileURLToPath(
	new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)
);
const DECK_PATH = fileURLToPath(
	new URL('../../src/lib/components/rb/Deck.svelte', import.meta.url)
);
const DROP_HELPER_PATH = fileURLToPath(
	new URL('../../src/lib/rb/deck-track-drop.ts', import.meta.url)
);

test('the drag start actually consults it, and does not refuse in silence', () => {
	const table = readFileSync(TABLE_PATH, 'utf8');
	const fn = table.slice(
		table.indexOf('function onRowDragStart('),
		table.indexOf('function onGripDragStart(')
	);
	assert.match(fn, /trackDragRefusal\(/, 'the drag start no longer asks why');
	assert.match(fn, /onrefused|pushToast/, 'a refused drag says nothing to the user');
});

/**
 * Review thread
 * https://github.com/maintainer/music-dj-tools/pull/945#discussion_r3917231707
 * (P1/BLOCKING): the assertion above only proves the two identifiers occur
 * somewhere inside the function body - it still passes if the handler is
 * unreachable, `onrefused` is not wired through BrowserPanel, or nothing ever
 * shows the user a toast. There is no component-mount harness in this repo to
 * render TrackTable/BrowserPanel and dispatch a real dragstart instead
 * (documented precedent: JobsDrawer.svelte's test header, "no component mount
 * infra (no jsdom, no @testing-library)"). Trace the actual chain by source
 * instead, closing each gap named above one at a time:
 *   1. the row's real DOM `ondragstart` reaches `onRowDragStart` (reachable,
 *      not dead code)
 *   2. the refusal branch specifically (not just the function generally)
 *      short-circuits to `onrefused?.(refusal)` before any drag starts
 *   3. `onrefused` is a declared prop, not a typo the compiler could not catch
 *   4. BrowserPanel's own `<TrackTable>` instantiation wires `onrefused` to
 *      `pushToast(reason, 'error')` - the actual user-visible toast
 */
describe('trackDragRefusal is wired end to end, TrackTable to BrowserPanel', () => {
	const table = readFileSync(TABLE_PATH, 'utf8');
	const panel = readFileSync(PANEL_PATH, 'utf8');

	it('a real row dragstart reaches onRowDragStart, not dead code', () => {
		assert.match(table, /ondragstart=\{\(e\) => onRowDragStart\(e, row\)\}/);
	});

	it('the refusal branch itself calls onrefused before returning, without starting the drag', () => {
		const fn = table.slice(
			table.indexOf('function onRowDragStart('),
			table.indexOf('function onRowDragEnd(')
		);
		const branch = fn.slice(fn.indexOf('if (refusal !== null)'), fn.indexOf('const ids ='));
		assert.match(branch, /event\.preventDefault\(\)/, 'a refused drag does not cancel the gesture');
		assert.match(branch, /onrefused\?\.\(refusal\)/, 'the refusal branch does not call onrefused');
		assert.match(branch, /return;/, 'falls through to starting the drag after refusing');
	});

	it('onrefused is a declared prop of TrackTable, not an unbound identifier', () => {
		assert.match(table, /onrefused\?:\s*\(reason: string\) => void/);
	});

	it('BrowserPanel wires onrefused on the real TrackTable to a real toast', () => {
		assert.match(
			panel,
			/<TrackTable[\s\S]{0,2000}?onrefused=\{\(reason\) => pushToast\(reason, 'error'\)\}/
		);
	});

	// Review thread https://github.com/maintainer/music-dj-tools/pull/945#discussion_r3919725005
	// (P2/NON-BLOCKING, found on the wiring above): the row's raw
	// is_streaming was the only thing this refusal checked. All Tracks rows
	// deliberately start row.is_streaming at null and hydrate the real value
	// into row.rb_meta later (same reason _loadOntoDeck already reads
	// `row.is_streaming ?? row.rb_meta?.is_streaming ?? false`) - so a
	// hydrated streaming row could drag-refuse silently correct by luck of
	// file_exists, or worse, be allowed to start a drag its own double-click
	// path would refuse.
	it('the drag refusal consults the hydrated rb_meta fallback, matching the load path', () => {
		const fn = table.slice(
			table.indexOf('function onRowDragStart('),
			table.indexOf('function onRowDragEnd(')
		);
		assert.match(
			fn,
			/is_streaming:\s*row\.is_streaming\s*\?\?\s*row\.rb_meta\?\.is_streaming\s*\?\?\s*false/,
			'drag refusal ignores a hydrated rb_meta.is_streaming, unlike the load path'
		);
	});

	it('beginTrackDrag carries file_exists and is_streaming for the source row', () => {
		const fn = table.slice(
			table.indexOf('function onRowDragStart('),
			table.indexOf('function onRowDragEnd(')
		);
		assert.match(fn, /beginTrackDrag\(ids,\s*\{/);
		assert.match(fn, /file_exists:\s*row\.file_exists/);
		assert.match(fn, /is_streaming:\s*row\.is_streaming\s*\?\?\s*row\.rb_meta\?\.is_streaming\s*\?\?\s*false/);
	});
});

describe('deck drop guards (STATE-10, issue #2451)', () => {
	const deck = readFileSync(DECK_PATH, 'utf8');
	const dropHelper = readFileSync(DROP_HELPER_PATH, 'utf8');

	it('onTrackDrop snapshots droppedRowFlags before endTrackDrag', () => {
		const fn = deck.slice(deck.indexOf('async function onTrackDrop('), deck.indexOf('</script>'));
		const flagsPos = fn.indexOf('droppedRowFlags(');
		const endPos = fn.indexOf('endTrackDrag()');
		assert.ok(flagsPos >= 0, 'onTrackDrop must read droppedRowFlags');
		assert.ok(endPos >= 0, 'onTrackDrop must call endTrackDrag');
		assert.ok(flagsPos < endPos, 'flags must be snapshotted before endTrackDrag clears them');
	});

	it('onTrackDrop delegates load guards to applyDeckTrackDrop before dispatching load', () => {
		const fn = deck.slice(deck.indexOf('async function onTrackDrop('), deck.indexOf('</script>'));
		const helperPos = fn.indexOf('applyDeckTrackDrop(');
		const loadPos = fn.indexOf("type: 'load'");
		assert.ok(helperPos >= 0, 'onTrackDrop must call applyDeckTrackDrop');
		assert.equal(loadPos, -1, 'onTrackDrop must not dispatch load directly');
	});

	it('deck-track-drop reuses trackDragRefusal instead of duplicating phrases', () => {
		assert.match(dropHelper, /trackDragRefusal/);
		assert.equal(dropHelper.includes("Open DJ can't play streaming services"), false);
		assert.equal(
			dropHelper.includes('cannot load: audio file missing on disk (broken link)'),
			false
		);
	});
});
