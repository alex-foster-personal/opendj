/**
 * SETNOTE-6340f/10: an @ mention is short linked text; selected songs are compared.
 *
 * [if] a song is mentioned or songs are selected [then] a 15-char link is saved and BPM/key compared
 */

import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let ed;
let cmp;
let EditorState;
let TextSelection;

before(async () => {
	ed = await loadTypeScriptModule('src/lib/set-notes/editor/index.ts');
	cmp = await loadTypeScriptModule('src/lib/set-notes/compare.ts');
	({ EditorState, TextSelection } = await import('prosemirror-state'));
});

const stateOf = (md) => EditorState.create({ doc: ed.parseNoteMarkdown(md) });

test('truncateLabel keeps 15 characters and marks the cut', () => {
	assert.equal(ed.truncateLabel('Short'), 'Short');
	assert.equal(ed.truncateLabel('webkit-fixture-a-128bpm'), 'webkit-fixture-\u2026');
	assert.equal(ed.truncateLabel('Example Tune Name (Long Mix)'), 'Example Tune Na\u2026');
});

test('an @ mention becomes linked @name text that round-trips as a markdown link', () => {
	const typed = stateOf('open with @webk');
	const at = typed.doc.textContent.indexOf('@') + 1;
	const range = { from: at, to: typed.doc.content.size - 1 };
	const state = typed.apply(typed.tr.setSelection(TextSelection.create(typed.doc, range.to)));
	const next = state.apply(ed.insertMentionLink(state, range, 'track:a1', 'webkit-fixture-a-128bpm'));
	const md = ed.serializeNoteMarkdown(next.doc);
	assert.equal(md, 'open with [@webkit-fixture-\u2026](track:a1) ');
	assert.equal(ed.serializeNoteMarkdown(ed.parseNoteMarkdown(md)), md);
	assert.deepEqual(ed.findMention(state), { query: 'webk', from: at, to: range.to });
	assert.equal(ed.findMention(next), null, 'a picked mention does not reopen the search');
});

test('trackIdsInSelection lists distinct linked songs in the selection only', () => {
	const state = stateOf('[@A](track:a1) then [[stem:b2:vocals|B]] and [@A again](track:a1) [site](https://x.example)');
	assert.deepEqual(ed.trackIdsInSelection(state), []);
	const all = state.apply(state.tr.setSelection(TextSelection.create(state.doc, 1, state.doc.content.size - 1)));
	assert.deepEqual(ed.trackIdsInSelection(all), ['a1', 'b2']);
	const first = state.apply(state.tr.setSelection(TextSelection.create(state.doc, 1, 3)));
	assert.deepEqual(ed.trackIdsInSelection(first), ['a1']);
});

test('compareTracks gives the tempo gap and Camelot fit, unknown stays unknown', () => {
	const c = cmp.compareTracks({ bpm: 120, key: '8A' }, { bpm: 126, key: '9A' });
	assert.equal(c.deltaBpm, 6);
	assert.equal(c.deltaPct, 5);
	assert.equal(c.keysMix, true);
	assert.equal(cmp.compareTracks({ bpm: 120, key: '8A' }, { bpm: 120, key: '2B' }).keysMix, false);
	assert.deepEqual(cmp.compareTracks({ bpm: null, key: null }, { bpm: 124 }), {
		deltaBpm: null,
		deltaPct: null,
		keysMix: null
	});
	assert.equal(cmp.signed(4), '+4.0');
	assert.equal(cmp.signed(-1.25), '-1.3');
});
