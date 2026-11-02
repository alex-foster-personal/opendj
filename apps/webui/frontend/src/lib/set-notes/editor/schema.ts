/**
 * The Set notes document model (SETNOTE-6340/6340d/6340e): exactly what the markdown
 * wire format in ./markdown.ts can carry, so a save never drops structure.
 *
 *   link       a mark on ordinary text, `[text](track:ID)`, `(stem:ID:STEM)`, `(https://..)`;
 *              an `@` mention is one of these, `[@Song name…](track:ID)`
 *   track_ref  an inline atom, `[[track:ID|label]]` / `[[stem:ID:STEM|label]]`, which
 *              agents and older notes write; it renders as link text too
 */
import { Schema, type DOMOutputSpec, type Node as PMNode } from 'prosemirror-model';

export type NoteTarget =
	| { kind: 'track'; stableId: string }
	| { kind: 'stem'; stableId: string; stem: string }
	| { kind: 'http'; url: string };

/** A stable id may not contain ':', '|', brackets, parentheses, a backslash or
 * whitespace, which keeps both token forms unambiguous without escapes. */
export const STABLE_ID = String.raw`[^\s:|\[\]()\\]+`;
const HREF_RE = new RegExp(String.raw`^(?:(track):(${STABLE_ID})|(stem):(${STABLE_ID}):([a-z]+)|(https?://[^\s()]+))$`);

export function parseHref(href: string): NoteTarget | null {
	const m = HREF_RE.exec(href);
	if (!m) return null;
	if (m[1]) return { kind: 'track', stableId: m[2] };
	if (m[3]) return { kind: 'stem', stableId: m[4], stem: m[5] };
	return { kind: 'http', url: m[6] };
}

export function refTarget(chip: PMNode): NoteTarget {
	const { stableId, stem } = chip.attrs as { stableId: string; stem: string | null };
	return stem ? { kind: 'stem', stableId, stem } : { kind: 'track', stableId };
}

/** What an inline node points at: a chip's track, a linked text run's href, else null. */
export function nodeTarget(node: PMNode): NoteTarget | null {
	if (node.type === noteSchema.nodes.track_ref) return refTarget(node);
	const href = node.isText ? noteSchema.marks.link.isInSet(node.marks)?.attrs.href : null;
	return href ? parseHref(href) : null;
}

const block = (tag: string): DOMOutputSpec => [tag, 0];

/** Pasted HTML keeps a link only when its target is one a note can hold. */
function _hrefAttrs(href: string | null, httpOnly: boolean): { href: string } | false {
	const target = parseHref(href ?? '');
	return target && (!httpOnly || target.kind === 'http') ? { href: href! } : false;
}

export const noteSchema = new Schema({
	nodes: {
		doc: { content: 'block+' },
		paragraph: { group: 'block', content: 'inline*', parseDOM: [{ tag: 'p' }], toDOM: () => block('p') },
		heading: {
			group: 'block',
			content: 'inline*',
			attrs: { level: { default: 1 } },
			defining: true,
			parseDOM: [1, 2, 3].map((level) => ({ tag: `h${level}`, attrs: { level } })),
			toDOM: (node) => block(`h${node.attrs.level}`)
		},
		bullet_list: { group: 'block', content: 'list_item+', parseDOM: [{ tag: 'ul' }], toDOM: () => block('ul') },
		list_item: { content: 'paragraph block*', defining: true, parseDOM: [{ tag: 'li' }], toDOM: () => block('li') },
		text: { group: 'inline' },
		track_ref: {
			group: 'inline',
			inline: true,
			atom: true,
			attrs: { stableId: {}, stem: { default: null }, label: { default: '' } },
			parseDOM: [
				{
					tag: 'span[data-stable-id]',
					getAttrs: (dom) => ({
						stableId: dom.getAttribute('data-stable-id'),
						stem: dom.getAttribute('data-stem'),
						label: dom.getAttribute('data-label') ?? ''
					})
				}
			],
			toDOM: (node) => {
				const { stableId, stem, label } = node.attrs;
				const shown = label.trim() || stableId;
				return [
					'span',
					{
						class: stem ? 'sn-chip sn-chip-stem' : 'sn-chip',
						'data-testid': 'set-notes-chip',
						'data-stable-id': stableId,
						'data-kind': stem ? 'stem' : 'track',
						...(stem ? { 'data-stem': stem } : {}),
						'data-label': label
					},
					stem ? `${shown} (${stem})` : shown
				];
			}
		}
	},
	marks: {
		strong: { parseDOM: [{ tag: 'strong' }, { tag: 'b' }], toDOM: () => ['strong', 0] },
		link: {
			attrs: { href: {} },
			inclusive: false,
			parseDOM: [
				{ tag: 'a[data-href]', getAttrs: (dom) => _hrefAttrs(dom.getAttribute('data-href'), false) },
				{ tag: 'a[href]', getAttrs: (dom) => _hrefAttrs(dom.getAttribute('href'), true) }
			],
			// No href attribute: the editor decides what a click does, never the browser.
			toDOM: (mark) => ['a', { class: 'sn-link', 'data-href': mark.attrs.href, 'data-testid': 'set-notes-link' }, 0]
		}
	}
});
