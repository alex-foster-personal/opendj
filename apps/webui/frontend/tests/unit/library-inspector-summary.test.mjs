// requirement: LIBWS-02
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/library/inspector-summary.ts');
});

function row(overrides) {
	return {
		stable_id: 'x',
		title: 't',
		artist: null,
		genre: null,
		key: null,
		bpm: null,
		rating: null,
		energy: null,
		comments: null,
		duration_ms: null,
		...overrides
	};
}

test('a field every row agrees on reads as that value', () => {
	assert.deepEqual(mod.readField(['House', 'House']), { kind: 'same', value: 'House' });
	assert.equal(mod.readoutText(mod.readField([3, 3])), '3');
});

test('differing values read as Multiple values, never one row standing in for all', () => {
	const readout = mod.readField(['House', 'Techno']);
	assert.equal(readout.kind, 'mixed');
	assert.equal(mod.readoutText(readout), 'Multiple values');
});

test('a value on only some rows is mixed, not same', () => {
	// The overshoot to guard: dropping blanks and calling the rest "same"
	// would show one genre as if it covered every selected track.
	const readout = mod.readField(['House', null, '  ']);
	assert.equal(readout.kind, 'mixed');
	assert.equal(readout.distinct, 2);
});

test('all blank reads as empty', () => {
	assert.deepEqual(mod.readField([null, undefined, '']), { kind: 'empty' });
	assert.equal(mod.readoutText({ kind: 'empty' }), '');
});

test('selection summary totals known durations and counts the unknown ones', () => {
	const summary = mod.summarizeSelection([
		row({ stable_id: 'a', duration_ms: 60_000, bpm: 124, key: '8A', genre: 'House' }),
		row({ stable_id: 'b', duration_ms: 3_600_000, bpm: 128, key: '1B', genre: 'House' }),
		row({ stable_id: 'c', bpm: 126, key: '8A', genre: 'House' })
	]);
	assert.equal(summary.count, 3);
	assert.equal(summary.total_duration_ms, 3_660_000);
	assert.equal(summary.unknown_duration_count, 1);
	assert.equal(mod.formatDuration(summary.total_duration_ms), '1:01:00');
	assert.equal(mod.formatBpmRange(summary.bpm_min, summary.bpm_max), '124.00 to 128.00');
	assert.deepEqual(summary.keys, ['1B', '8A']);
	assert.equal(summary.genre.kind, 'same');
});

test('no known duration gives a null total, not zero', () => {
	const summary = mod.summarizeSelection([row({}), row({ stable_id: 'b' })]);
	assert.equal(summary.total_duration_ms, null);
	assert.equal(summary.unknown_duration_count, 2);
	assert.equal(mod.formatBpmRange(summary.bpm_min, summary.bpm_max), '');
});

test('camelot keys sort by number then letter, unparsed last', () => {
	assert.deepEqual(['12A', 'Fm', '2B', '2A', '10A'].sort(mod.compareCamelot), [
		'2A',
		'2B',
		'10A',
		'12A',
		'Fm'
	]);
});

test('source labels name the writer and never fold an unknown one into a known label', () => {
	assert.equal(mod.sourceLabel('rekordbox'), 'RB');
	assert.equal(mod.sourceLabel('own-analysis'), 'OWN');
	assert.equal(mod.sourceLabel('webui'), 'YOU');
	assert.equal(mod.sourceLabel('lexicon'), 'LEXICON');
	assert.throws(() => mod.sourceLabel(' '), /empty/);
});

test('the Library page lazily mounts the Crate Desk built on the shared browser', () => {
	const page = readFileSync(`${SRC}/routes/+page.svelte`, 'utf8');
	const desk = readFileSync(`${SRC}/lib/components/library/CrateDesk.svelte`, 'utf8');
	// Dynamic, never static: a static import puts the whole browser in the
	// first paint of "/", which every cold open downloads before Gig.
	assert.match(page, /import\('\$lib\/components\/library\/CrateDesk\.svelte'\)/);
	assert.doesNotMatch(page, /^\s*import\s+\w+\s+from\s+'[^']*(CrateDesk|BrowserPanel)/m);
	// The desk tells the browser it is in Library mode (LIBWS-01/03) through a
	// prop, so the browser itself never imports the workspace module.
	assert.match(desk, /<BrowserPanel \{library\} \/>/);
	assert.match(desk, /const library = \{ refusal: LIBRARY_DECK_LOAD_REFUSAL, publish: publishLibrarySelection \}/);
	const panel = readFileSync(`${SRC}/lib/components/rb/BrowserPanel.svelte`, 'utf8');
	assert.match(panel, /if \(library !== null\) \{\s*pushToast\(library\.refusal, 'info'\);\s*return;/);
	assert.match(panel, /libraryMode=\{library !== null\}/);
	assert.doesNotMatch(panel, /library-workspace/);
	assert.match(desk, /<TrackInspector \/>/);
});

test('a save that resolves after the selection moved never lands on the new track', () => {
	const inspector = readFileSync(`${SRC}/lib/components/library/TrackInspector.svelte`, 'utf8');
	const body = inspector.slice(inspector.indexOf('async function savePatch'), inspector.indexOf('function keepMine'));
	// The row and the load are captured before the await...
	assert.match(body, /const seq = loadSeq;\s*const row = single !== null && single\.stable_id === track\.stable_id \? single : null;[\s\S]*await patchTrack/);
	// ...the row update uses the captured row, never the live selection...
	assert.match(body, /if \(row !== null && patch\.rating !== undefined\) row\.rating = /);
	// Notes are not the Comments column, so a notes save never writes the row's comments.
	assert.doesNotMatch(body, /row\.comments =/);
	assert.doesNotMatch(body, /single\.(rating|comments) =/);
	// ...and the inspector's own state is written only while the same load shows.
	assert.match(body, /if \(seq !== loadSeq\) return;\s*track = res\.track;/);
	assert.match(body, /catch \(exc\) \{\s*if \(seq !== loadSeq\) return;/);
});

test('every inspector field the server can attribute shows its source chip', () => {
	const inspector = readFileSync(`${SRC}/lib/components/library/TrackInspector.svelte`, 'utf8');
	// LIBWS-02: a chip for each displayed field that has provenance. Rating, BPM,
	// key, genre, energy and comments are track_fields entries; time and plays
	// come from the tracks row and carry no provenance.
	for (const field of ['rating', 'bpm', 'key', 'genre', 'energy', 'comments']) {
		assert.match(
			inspector,
			new RegExp(`\\{#if sourceOf\\('${field}'\\) !== null\\}<span class="src" title=\\{sourceTitle\\('${field}'\\)\\}>\\{sourceOf\\('${field}'\\)\\}</span>\\{/if\\}`),
			`no source chip for ${field}`,
		);
	}
});

test('a selection change drops a conflict raised on the previous track', () => {
	const inspector = readFileSync(`${SRC}/lib/components/library/TrackInspector.svelte`, 'utf8');
	const reset = inspector.slice(inspector.indexOf('const sid = singleId;'), inspector.indexOf('if (sid === null) return;'));
	assert.match(reset, /conflictServer = null;/);
	assert.match(reset, /pendingPatch = null;/);
});

test('the inspector follows a whole-collection search hit, not only playlist rows', () => {
	const panel = readFileSync(`${SRC}/lib/components/rb/BrowserPanel.svelte`, 'utf8');
	assert.match(
		panel,
		/library\.publish\(pane\.selected_ids, wholeCollectionActive \? pane\.search_results : pane\.rows, pane\.title\);/
	);
});

test('the Library mode track menu offers no worker-starting items', () => {
	const menu = readFileSync(`${SRC}/lib/components/rb/browser/TrackContextMenu.svelte`, 'utf8');
	// The registry owns the gate (CTXMENU-01); the host only reports the mode,
	// and the registry unit suite proves which items it drops.
	assert.match(menu, /workspace: libraryWorkspace\.active \? 'library' : 'performance'/);
	assert.doesNotMatch(menu, /\.filter\(/, 'the host must not filter registry items');
});

test('inspector saves run one at a time, each with its own patch', () => {
	const inspector = readFileSync(`${SRC}/lib/components/library/TrackInspector.svelte`, 'utf8');
	const queue = inspector.slice(inspector.indexOf('function applyPatch'), inspector.indexOf('async function savePatch'));
	// Each save waits for the one before it (so it sends the etag that one
	// returned), and a save queued for a track the selection left is dropped.
	assert.match(queue, /const seq = loadSeq;\s*const run = saveChain\.then\(\(\) => \(seq === loadSeq \? savePatch\(patch\) : undefined\)\);/);
	assert.match(queue, /saveChain = run\.catch\(\(\) => \{\}\);/);
	assert.doesNotMatch(queue, /await patchTrack/);
});

test('a multi-select during whole-collection search names the search hits for every consumer', () => {
	const panel = readFileSync(`${SRC}/lib/components/rb/BrowserPanel.svelte`, 'utf8');
	// The selection itself is built from the rows on screen, so bulk edit, the
	// context menu and the inspector all read the same search hits.
	assert.match(
		panel,
		/const orderedRows = range \|\| wholeCollectionActive\s*\? renderedRows\.map\(\(r\) => \(\{ stable_id: r\.stable_id, order: r\.order \}\)\)/
	);
	// A playlist refresh under the search never rebuilds it from playlist rows.
	assert.match(panel, /if \(!\(p === pane && wholeCollectionActive\)\) pruneSelection\(p, result\.rows\);/);
});

test('Library mode starts no ANLZ decode from browsing', () => {
	const panel = readFileSync(`${SRC}/lib/components/rb/BrowserPanel.svelte`, 'utf8');
	assert.match(panel, /if \(library === null\) ensureAnlzPrefetch\(row\.stable_id\);/);
	assert.equal(panel.split('ensureAnlzPrefetch(').length, 2, 'another ANLZ prefetch call is ungated');
});

test('an open conflict holds the save queue and keeps its own patch', () => {
	const inspector = readFileSync(`${SRC}/lib/components/library/TrackInspector.svelte`, 'utf8');
	const save = inspector.slice(inspector.indexOf('async function savePatch'), inspector.indexOf('function keepMine'));
	// The conflicted save does not settle until the dialog answers, so the next
	// queued save cannot run on the conflict etag or replace pendingPatch.
	assert.match(save, /conflictServer = exc\.current;\s*etag = exc\.etag;\s*const next = await new Promise<Patch \| null>\(\(resolve\) => \{\s*answerConflict = resolve;/);
	assert.match(save, /if \(next !== null && seq === loadSeq\) await savePatch\(next\);/);
	const answers = inspector.slice(inspector.indexOf('function keepMine'), inspector.indexOf('function addTag'));
	// Every answer releases the queue, and none enqueues behind the held save
	// (that would wait on itself).
	assert.match(answers, /function keepMine[\s\S]*?settleConflict\(patch\);/);
	assert.match(answers, /function takeTheirs[\s\S]*?settleConflict\(null\);/);
	assert.match(answers, /settleConflict\(\{ tags_add: merged \}\);/);
	assert.doesNotMatch(answers, /applyPatch\(/);
	// A selection change releases it too, and the queued saves then drop.
	const reset = inspector.slice(inspector.indexOf('const sid = singleId;'), inspector.indexOf('if (sid === null) return;'));
	assert.match(reset, /settleConflict\(null\);/);
});

test('Library mode mounts no recorder and no MIDI audio entry', () => {
	const panel = readFileSync(`${SRC}/lib/components/rb/BrowserPanel.svelte`, 'utf8');
	assert.match(panel, /<PerformanceRecorderRail \{source\} onspotify=\{selectSpotifySource\} oncollection=\{selectCollectionSource\} recordable=\{library === null\} \/>/);
	const tray = panel.slice(panel.indexOf('{#if library === null}\n\t\t\t\t<button\n\t\t\t\t\ttype="button"\n\t\t\t\t\tclass="tray-midi"'));
	assert.ok(tray.length > 0 && tray.indexOf('headphone_output_acquire') < tray.indexOf('{/if}'), 'the MIDI entry is not gated');
	const rail = readFileSync(`${SRC}/lib/components/rb/browser/PerformanceRecorderRail.svelte`, 'utf8');
	assert.match(rail, /if \(recordable\) void refreshRecorderStatus\(\);/);
	assert.match(rail, /^\t\{recordable\}$/m);
	const icons = readFileSync(`${SRC}/lib/components/rb/browser/IconRail.svelte`, 'utf8');
	assert.match(icons, /entry\.action !== 'record' \|\| \(recordable && setRecordingEnabled\(\)\)/);
});

test('every selection change, to none or several tracks too, invalidates saves in flight', () => {
	const inspector = readFileSync(`${SRC}/lib/components/library/TrackInspector.svelte`, 'utf8');
	const reset = inspector.slice(inspector.indexOf('const sid = singleId;'), inspector.indexOf('getTrack(sid)'));
	// The sequence moves BEFORE the early return, or a deselect would leave an
	// old save able to reopen its conflict over the new view.
	assert.ok(reset.indexOf('const seq = ++loadSeq;') !== -1, 'the reset does not bump loadSeq');
	assert.ok(reset.indexOf('const seq = ++loadSeq;') < reset.indexOf('if (sid === null) return;'));
	assert.equal(inspector.split('++loadSeq').length, 2, 'loadSeq moves in one place only');
});

test('the inspector stars stay inert until the track and its etag have loaded', () => {
	const inspector = readFileSync(`${SRC}/lib/components/library/TrackInspector.svelte`, 'utf8');
	assert.match(inspector, /disabledReason=\{track === null \? "[^"]+" : null\}/);
	const stars = readFileSync(`${SRC}/lib/components/rb/browser/RatingStars.svelte`, 'utf8');
	assert.match(stars, /title=\{disabledReason \?\? `Set rating \$\{n\}`\}\s*disabled=\{disabledReason !== null\}/);
});

test('Library mode starts no grid scan, no row preview audio and no recorder polling', () => {
	const table = readFileSync(`${SRC}/lib/components/rb/browser/TrackTable.svelte`, 'utf8');
	assert.match(table, /if \(!libraryMode\) void ensureGridQualityScan\(\);/);
	assert.equal(table.split('ensureGridQualityScan()').length, 2, 'another grid scan call is ungated');
	const panel = readFileSync(`${SRC}/lib/components/rb/BrowserPanel.svelte`, 'utf8');
	assert.match(panel, /onpreviewseek=\{library === null \? previewSeek : undefined\}/);
	const rail = readFileSync(`${SRC}/lib/components/rb/browser/PerformanceRecorderRail.svelte`, 'utf8');
	assert.match(rail, /if \(after === null \|\| !recordable\) return;\s*const timer = setTimeout\(\(\) => void refreshRecorderStatus\(\), after\);/);
	assert.equal(rail.split('void refreshRecorderStatus()').length, 3, 'a third recorder status read is ungated');
});
