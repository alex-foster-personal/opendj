/**
 * SETNOTE-6340c/04: the Set notes markdown wire format and the editor document.
 *
 * [if] a note is parsed into the editor doc and serialized back [then] canonical
 * markdown is unchanged and chips, links, bold and escapes land on the right nodes
 */

import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let md;

before(async () => {
	md = await loadTypeScriptModule('src/lib/set-notes/editor/markdown.ts');
});

const json = (text) => JSON.parse(JSON.stringify(md.parseNoteMarkdown(text).toJSON()));
const roundTrip = (text) => md.serializeNoteMarkdown(md.parseNoteMarkdown(text));
const inline = (text) => json(text).content[0].content;

test('canonical notes round-trip byte for byte', () => {
	for (const text of [
		'',
		'# Opener\n## Build\n### Peak',
		'- one\n- two\n\n- separate list',
		'open [[track:abc123|Artist - Title]] then [[stem:xyz:vocals|Acapella]] and [[track:def]].',
		'**peak** time [webkit](track:x1) and [**hook**](stem:y:drums) at [site](https://example.com/a?b=1)',
		'literal \\*star\\* \\[x\\] back\\\\slash',
		'\\# not a heading\n  \\- not an item\n\\## nor this',
		'- [[track:a|A]] **then** [b](track:b)\n# after',
		'   ',
		'a line\n\nanother'
	]) {
		assert.equal(roundTrip(text), text, JSON.stringify(text));
	}
});

test('one line is one block: headings, bullets grouped, empty paragraphs kept', () => {
	const doc = json('# H\n- a\n- b\n\ntext\n### Small');
	assert.deepEqual(
		doc.content.map((n) => [n.type, n.attrs?.level ?? null]),
		[
			['heading', 1],
			['bullet_list', null],
			['paragraph', null],
			['paragraph', null],
			['heading', 3]
		]
	);
	assert.equal(doc.content[1].content.length, 2);
	assert.equal(doc.content[2].content, undefined);
});

test('legacy [[track]] and [[stem]] tokens become chip atoms', () => {
	assert.deepEqual(inline('[[track:abc|A - B]] [[stem:xyz:vocals]]'), [
		{ type: 'track_ref', attrs: { stableId: 'abc', stem: null, label: 'A - B' } },
		{ type: 'text', text: ' ' },
		{ type: 'track_ref', attrs: { stableId: 'xyz', stem: 'vocals', label: '' } }
	]);
});

test('markdown links to tracks, stems and the web become link marks', () => {
	const nodes = inline('[go](track:t1) [v](stem:t2:vocals) [w](https://e.com/x)');
	const hrefs = nodes.filter((n) => n.marks).map((n) => [n.text, n.marks[0].attrs.href]);
	assert.deepEqual(hrefs, [
		['go', 'track:t1'],
		['v', 'stem:t2:vocals'],
		['w', 'https://e.com/x']
	]);
});

test('bold marks text, and bold inside a link keeps both marks', () => {
	assert.deepEqual(inline('a **b** c'), [
		{ type: 'text', text: 'a ' },
		{ type: 'text', marks: [{ type: 'strong' }], text: 'b' },
		{ type: 'text', text: ' c' }
	]);
	const [linked] = inline('[**x**](track:t)');
	assert.deepEqual(linked.marks.map((m) => m.type).sort(), ['link', 'strong']);
});

test('escapes are honoured on parse and written back on serialize', () => {
	assert.deepEqual(inline('\\*\\*not bold\\*\\* \\[\\[track:a\\]\\]'), [
		{ type: 'text', text: '**not bold** [[track:a]]' }
	]);
	assert.equal(json('\\# x').content[0].type, 'paragraph');
});

test('malformed tokens and links stay literal text', () => {
	for (const text of [
		'a [[track:x:vocals]] b',
		'[[stem:y]]',
		'[[track:]]',
		'[a](ftp://x)',
		'[b](track:)',
		'**open'
	]) {
		const nodes = inline(text);
		assert.deepEqual(nodes, [{ type: 'text', text }], text);
		assert.equal(md.serializeNoteMarkdown(md.parseNoteMarkdown(roundTrip(text))), roundTrip(text));
	}
});

test('non-canonical input normalizes once and is then stable', () => {
	for (const [input, canonical] of [
		['* star item', '- star item'],
		['line\r\nnext', 'line\nnext'],
		['**[a](track:z)**', '[**a**](track:z)']
	]) {
		assert.equal(roundTrip(input), canonical);
		assert.equal(roundTrip(canonical), canonical);
	}
});
