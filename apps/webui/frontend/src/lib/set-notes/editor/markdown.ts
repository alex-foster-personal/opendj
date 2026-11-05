/**
 * The Set notes wire format (SETNOTE-6340c/04), the one agents and the CLI read
 * and write, to and from the editor document. One line is one block:
 *
 *   # / ## / ### text      heading
 *   - text  (or * text)    bullet item; consecutive items are one list
 *   anything else          paragraph (an empty line is an empty paragraph)
 *
 * Inline: **bold**, [[track:ID|label]] and [[stem:ID:STEM|label]] chips, and
 * [text](track:ID) / [text](stem:ID:STEM) / [text](https://..) links. A
 * backslash escapes \ * [ ] in text (and a leading # or - that would otherwise
 * start a block). Anything malformed stays literal text, so
 * serializeNoteMarkdown(parseNoteMarkdown(md)) === md for canonical input.
 */
import type { Mark, Node as PMNode } from 'prosemirror-model';
import { noteSchema, parseHref, STABLE_ID } from './schema';

const { nodes, marks } = noteSchema;
const HEADING_RE = /^(#{1,3}) (.*)$/;
const ITEM_RE = /^\s*[-*] (.*)$/;
const BLOCK_START_RE = /^(?:#{1,3} |\s*[-*] )/;
const INLINE_RE = new RegExp(
	[
		String.raw`\\([\\*\[\]#-])`,
		String.raw`\[\[(track|stem):(${STABLE_ID})(?::([a-z]+))?(?:\|([^\]\n]*))?\]\]`,
		String.raw`\[((?:\\.|[^\]\\\n])*)\]\(([^\s()]+)\)`,
		String.raw`(\*\*)`
	].join('|'),
	'gy'
);

type Piece = PMNode | 'strong';

function _tokens(text: string, inLink: boolean): Piece[] {
	const out: Piece[] = [];
	let plain = '';
	const flush = (): void => {
		if (plain) out.push(noteSchema.text(plain));
		plain = '';
	};
	for (let i = 0; i < text.length;) {
		INLINE_RE.lastIndex = i;
		const m = INLINE_RE.exec(text);
		const [, escaped, kind, stableId, stem, label, linkText, href, bold] = m ?? [];
		const target = href !== undefined && !inLink ? parseHref(href) : null;
		if (escaped !== undefined) {
			plain += escaped;
		} else if (kind !== undefined && (kind === 'stem') === (stem !== undefined)) {
			flush();
			out.push(nodes.track_ref.create({ stableId, stem: stem ?? null, label: label ?? '' }));
		} else if (target) {
			flush();
			const link = marks.link.create({ href });
			for (const p of _tokens(linkText, true))
				out.push(p === 'strong' ? p : p.mark(link.addToSet(p.marks)));
		} else if (bold !== undefined) {
			flush();
			out.push('strong');
		} else {
			plain += text[i];
			i += 1;
			continue;
		}
		i += m![0].length;
	}
	flush();
	return out;
}

/** Pairs the ** delimiters; an unpaired last one is literal text. */
function _inline(text: string): PMNode[] {
	const pieces = _tokens(text, false);
	const unpaired =
		pieces.filter((p) => p === 'strong').length % 2 === 1 ? pieces.lastIndexOf('strong') : -1;
	let bold = false;
	const out: PMNode[] = [];
	pieces.forEach((p, i) => {
		if (p !== 'strong') out.push(bold ? p.mark(marks.strong.create().addToSet(p.marks)) : p);
		else if (i === unpaired) out.push(noteSchema.text('**'));
		else bold = !bold;
	});
	return out;
}

export function parseNoteMarkdown(md: string): PMNode {
	const blocks: PMNode[] = [];
	let items: PMNode[] = [];
	const endList = (): void => {
		if (items.length > 0) blocks.push(nodes.bullet_list.create(null, items));
		items = [];
	};
	for (const line of md.split(/\r?\n/)) {
		const heading = HEADING_RE.exec(line);
		const item = heading ? null : ITEM_RE.exec(line);
		if (item) {
			items.push(nodes.list_item.create(null, nodes.paragraph.create(null, _inline(item[1]))));
			continue;
		}
		endList();
		blocks.push(
			heading
				? nodes.heading.create({ level: heading[1].length }, _inline(heading[2]))
				: nodes.paragraph.create(null, _inline(line))
		);
	}
	endList();
	return nodes.doc.create(null, blocks);
}

const _escape = (text: string): string => text.replace(/[\\*[\]]/g, '\\$&');

function _ref(node: PMNode): string {
	const { stableId, stem, label } = node.attrs;
	const clean = String(label).replace(/[\]\n]/g, ' ');
	return `[[${stem ? `stem:${stableId}:${stem}` : `track:${stableId}`}${clean ? `|${clean}` : ''}]]`;
}

const _hrefOf = (node: PMNode): string | null =>
	node.isText ? (node.marks.find((m: Mark) => m.type === marks.link)?.attrs.href ?? null) : null;

function _serializeInline(block: PMNode): string {
	let out = '';
	let link: string | null = null;
	let bold = false;
	block.forEach((child) => {
		const href = _hrefOf(child);
		const strong = marks.strong.isInSet(child.marks) !== undefined;
		if (href !== link) {
			if (bold) out += '**';
			bold = false;
			if (link !== null) out += `](${link})`;
			if (href !== null) out += '[';
			link = href;
		}
		if (strong !== bold) out += '**';
		bold = strong;
		out += child.isText ? _escape(child.text!) : child.type === nodes.track_ref ? _ref(child) : '';
	});
	if (bold) out += '**';
	if (link !== null) out += `](${link})`;
	return out;
}

function _serializeBlock(node: PMNode, lines: string[]): void {
	if (node.type === nodes.bullet_list) {
		node.forEach((item) =>
			item.forEach((child, _offset, index) => {
				if (index === 0) lines.push(`- ${_serializeInline(child)}`);
				else _serializeBlock(child, lines);
			})
		);
		return;
	}
	const text = _serializeInline(node);
	if (node.type === nodes.heading) lines.push(`${'#'.repeat(node.attrs.level)} ${text}`);
	else lines.push(BLOCK_START_RE.test(text) ? text.replace(/[#*-]/, '\\$&') : text);
}

export function serializeNoteMarkdown(doc: PMNode): string {
	const lines: string[] = [];
	doc.forEach((node) => _serializeBlock(node, lines));
	return lines.join('\n');
}
