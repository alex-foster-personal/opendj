// requirement: SETNOTE-10457
/**
 * SETNOTE-6340d/05: what a click in the Set notes editor follows.
 *
 * [if] a link or chip is Cmd/Ctrl-clicked [then] its target is followed, plain clicks edit
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
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

// SETNOTE-10457: the right-click menu targets a chip the same way it targets a text link.
// [if] right-clicking a [[track:ID|label]] chip gives no Open / load target [then] broken
// [if] a [[stem:ID:STEM|label]] chip does not target that stem [then] broken
// [if] a chip offers Remove link, or a missing track's Open is not inert with its reason [then] broken
test('menuTargetAt resolves a chip and a text link to the same NoteTarget shape', () => {
	const c = chip('Opener');
	assert.deepEqual(ed.menuTargetAt(c.state, c.pos), {
		target: { kind: 'track', stableId: 'a1' },
		link: null
	});
	assert.deepEqual(
		ed.menuTargetAt(c.state, c.pos + 1).target,
		{ kind: 'track', stableId: 'a1' },
		'just after the chip'
	);
	const s = chip('Acap');
	assert.deepEqual(ed.menuTargetAt(s.state, s.pos).target, {
		kind: 'stem',
		stableId: 's2',
		stem: 'vocals'
	});
	const l = linkText('webkit');
	const viaLink = ed.menuTargetAt(l.state, l.pos);
	assert.deepEqual(viaLink.target, { kind: 'track', stableId: 'b2' });
	assert.equal(viaLink.link.href, 'track:b2', 'a text link keeps its range for Remove link');
	const plain = at((n) => n.isText && n.text.includes('end'));
	assert.equal(ed.menuTargetAt(plain.state, plain.pos + plain.node.text.indexOf('end') + 1), null);
});

test('a missing track is unknown until the track state has checked it', () => {
	const tracks = ed.createTrackState(async () => false);
	assert.equal(tracks.problem({ kind: 'track', stableId: 'gone' }), null, 'unknown until checked');
});
