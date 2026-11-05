/**
 * SETNOTE-6340d/05: what a click in the Set notes editor follows.
 *
 * [if] a link or chip is Cmd/Ctrl-clicked [then] its target is followed, plain clicks edit
 */

import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let ed;

before(async () => {
	ed = await loadTypeScriptModule('src/lib/set-notes/editor/index.ts');
});

const NOTE =
	'open [[track:a1|Opener]] [[stem:s2:vocals|Acap]] then [webkit](track:b2) or [site](https://x.example/p) end';

/** The real parsed note plus the position inside the first node matching `pick`. */
function at(pick) {
	const doc = ed.parseNoteMarkdown(NOTE);
	let found = null;
	doc.descendants((node, pos) => {
		if (found === null && pick(node)) found = { node, pos: node.isText ? pos + 1 : pos };
	});
	assert.ok(found, 'fixture node exists');
	return { state: { doc }, ...found };
}

const chip = (label) => at((n) => n.type.name === 'track_ref' && n.attrs.label === label);
const linkText = (text) => at((n) => n.isText && n.text === text);

test('a chip is followed only on Cmd/Ctrl-click, like a text link', () => {
	const c = chip('Opener');
	assert.equal(ed.clickTarget(c.state, c.pos, c.node, false), null);
	assert.deepEqual(ed.clickTarget(c.state, c.pos, c.node, true), { kind: 'track', stableId: 'a1' });
	const s = chip('Acap');
	assert.deepEqual(ed.clickTarget(s.state, s.pos, s.node, true), {
		kind: 'stem',
		stableId: 's2',
		stem: 'vocals'
	});
});

test('a text link is followed only on Cmd/Ctrl-click, so a plain click stays editable', () => {
	const track = linkText('webkit');
	assert.equal(ed.clickTarget(track.state, track.pos, null, false), null);
	assert.deepEqual(ed.clickTarget(track.state, track.pos, null, true), {
		kind: 'track',
		stableId: 'b2'
	});
	const web = linkText('site');
	assert.deepEqual(ed.clickTarget(web.state, web.pos, null, true), {
		kind: 'http',
		url: 'https://x.example/p'
	});
});

test('a Cmd/Ctrl-click on plain text follows nothing', () => {
	const plain = at((n) => n.isText && n.text.includes('end'));
	assert.equal(ed.clickTarget(plain.state, plain.pos + 2, null, true), null);
});

test('linkAt returns the whole run of the link under the click', () => {
	const { state, pos } = linkText('webkit');
	const run = ed.linkAt(state, pos);
	assert.equal(run.href, 'track:b2');
	assert.equal(state.doc.textBetween(run.from, run.to), 'webkit');
});
