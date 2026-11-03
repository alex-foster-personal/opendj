// requirement: LIBPG-7694e
// A USER deck load stops the headphone preview in the same turn as the
// gesture; AutoPlay's own load does not (Preview pin 104cf3b626fd). These pin
// where the silence sits and which source it skips. PVCUE-01's tests in
// preview-cue-stops-on-deck-load.test.mjs still pin the refusal ordering.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

const read = (path) => readFileSync(new URL(`../../src/${path}`, import.meta.url), 'utf8');

function fnBody(source, header) {
	const start = source.indexOf(header);
	assert.notEqual(start, -1, `${header} not found`);
	return source.slice(start, source.indexOf('\n}\n', start));
}

test('an AutoPlay load does not silence the preview inside _execute', () => {
	// if every load silences, AutoPlay loading the next track cuts the DJ's audition - broken.
	const body = fnBody(read('lib/rb/performance-ipc.svelte.ts'), 'async function _execute(');
	assert.match(body, /if \(deckCommandSource\(command\.deck\) !== 'autoplay-handoff'\) silencePreviewCue\(\);/);
});

test('_loadStopsPreview skips AutoPlay, refused master loads and loads the engine would refuse', () => {
	const ipc = read('lib/rb/performance-ipc.svelte.ts');
	const body = fnBody(ipc, 'function _loadStopsPreview(');
	assert.match(body, /command\.type === 'load'/);
	assert.match(body, /source !== 'autoplay-handoff'/);
	assert.match(body, /!\(command\.refuseIfMaster === true && getDeckState\(command\.deck\)\.is_master\)/);
	assert.match(body, /_deckAcceptsLoadNow\(command\.deck\)/);
	// Control: a user load still stops it, so the predicate is not simply false.
	assert.doesNotMatch(body, /source === 'user-ui'/);
});

test('a user load silences before the deck queue, not after AutoPlay commands ahead of it', () => {
	const body = fnBody(read('lib/rb/performance-ipc.svelte.ts'), 'async function _dispatchUnknown(');
	const silence = body.indexOf('if (_loadStopsPreview(command, source)) silencePreviewCue();');
	const queue = body.indexOf('performanceCommandQueueScopes(command)');
	assert.ok(silence !== -1 && queue !== -1 && silence < queue, 'the silence precedes the deck queue');
});

test('the library load path silences in the gesture turn, before it dispatches', () => {
	const panel = read('lib/components/rb/BrowserPanel.svelte');
	const silence = panel.indexOf('silencePreviewForUserLoad(target);');
	assert.notEqual(silence, -1);
	const dispatch = panel.indexOf('await dispatchPerformanceCommand(', silence);
	assert.ok(dispatch !== -1 && dispatch - silence < 800, 'the silence sits right before the load dispatch');
	// Same turn: nothing between the silence and the dispatch awaits (main's
	// KEYSHIFT-03 snapshot sits between them and is synchronous).
	assert.equal(panel.slice(silence, dispatch).includes('await '), false, 'no await before the dispatch');
	const masterCheck = panel.lastIndexOf('decks[target].is_master', silence);
	assert.ok(masterCheck !== -1 && silence - masterCheck < 600, 'a refused master load returns first');
});

test('deckCommandSource reads the per-deck register withDeckCommandSource sets', () => {
	const log = read('lib/rb/deck-stop-log.ts');
	assert.match(fnBody(log, 'export function deckCommandSource('), /return _commandSource\[deck\];/);
});

test('deckCommandSource names the running source per deck and clears after (runtime)', async () => {
	const { deckCommandSource, withDeckCommandSource } = await loadTypeScriptModule('src/lib/rb/deck-stop-log.ts');
	assert.equal(deckCommandSource(1), null);
	let inside = null;
	let other = 'unset';
	await withDeckCommandSource([1], 'autoplay-handoff', async () => {
		inside = deckCommandSource(1);
		other = deckCommandSource(2);
	});
	assert.equal(inside, 'autoplay-handoff');
	assert.equal(other, null, 'another deck does not inherit the source');
	assert.equal(deckCommandSource(1), null, 'the register is restored after the command');
});
