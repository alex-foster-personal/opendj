/** Document edits and lookups the Set notes popovers and menus run. */
import { TextSelection, type EditorState, type Transaction } from 'prosemirror-state';
import { nodeTarget, noteSchema } from './schema';

type Range = { from: number; to: number };

const WORD_CHAR = /[\p{L}\p{N}_'-]/u;
/** An `@` mention shows only this much of the song name, so the note stays prose. */
export const MENTION_CHARS = 15;

export function truncateLabel(text: string, max = MENTION_CHARS): string {
	const t = text.trim();
	return t.length > max ? `${t.slice(0, max).trimEnd()}\u2026` : t;
}

/** Replaces `range` (the `@query`) with `@name` linked to `href`, then an unlinked space. */
export function insertMentionLink(
	state: EditorState,
	range: Range,
	href: string,
	name: string
): Transaction {
	const link = noteSchema.marks.link.create({ href });
	const nodes = [noteSchema.text(`@${truncateLabel(name)}`, [link]), noteSchema.text(' ')];
	return state.tr.replaceWith(range.from, range.to, nodes).scrollIntoView();
}

/** Distinct track ids linked anywhere in a non-empty selection, in document order. */
export function trackIdsInSelection(state: Pick<EditorState, 'doc' | 'selection'>): string[] {
	const { from, to, empty } = state.selection;
	const ids = new Set<string>();
	if (!empty) {
		state.doc.nodesBetween(from, to, (node) => {
			const target = node.isInline ? nodeTarget(node) : null;
			if (target && target.kind !== 'http') ids.add(target.stableId);
		});
	}
	return [...ids];
}

/** The word around `pos` in its textblock, or null between words. */
export function wordAt(state: EditorState, pos: number): Range | null {
	const $pos = state.doc.resolve(pos);
	const block = $pos.parent;
	if (!block.isTextblock) return null;
	// One character per position: an atom chip reads as U+FFFC, never a word char.
	const text = block.textBetween(0, block.content.size, undefined, '\ufffc');
	let a = $pos.parentOffset;
	let b = a;
	while (a > 0 && WORD_CHAR.test(text[a - 1])) a--;
	while (b < text.length && WORD_CHAR.test(text[b])) b++;
	return a < b ? { from: $pos.start() + a, to: $pos.start() + b } : null;
}

/** Selects the word at `pos` when nothing is selected; the right-click menu acts on it. */
export function selectWordAt(state: EditorState, pos: number | null): Transaction | null {
	if (!state.selection.empty || pos === null) return null;
	const word = wordAt(state, pos);
	return word ? state.tr.setSelection(TextSelection.create(state.doc, word.from, word.to)) : null;
}

export function linkRange(state: EditorState, range: Range, href: string): Transaction {
	return state.tr.addMark(range.from, range.to, noteSchema.marks.link.create({ href }));
}

export function unlinkRange(state: EditorState, range: Range): Transaction {
	return state.tr.removeMark(range.from, range.to, noteSchema.marks.link);
}

/** The whole run of one link around `pos`, or null when `pos` is not on a link. */
export function linkAt(
	state: Pick<EditorState, 'doc'>,
	pos: number
): (Range & { href: string }) | null {
	const $pos = state.doc.resolve(pos);
	const block = $pos.parent;
	const linkType = noteSchema.marks.link;
	let i = $pos.index();
	let mark = i < block.childCount ? linkType.isInSet(block.child(i).marks) : undefined;
	if (!mark && $pos.textOffset === 0 && i > 0) mark = linkType.isInSet(block.child(--i).marks);
	if (!mark) return null;
	const link = mark;
	const has = (k: number): boolean =>
		k >= 0 && k < block.childCount && link.isInSet(block.child(k).marks);
	let first = i;
	let last = i;
	while (has(first - 1)) first--;
	while (has(last + 1)) last++;
	let from = $pos.start();
	for (let k = 0; k < first; k++) from += block.child(k).nodeSize;
	let to = from;
	for (let k = first; k <= last; k++) to += block.child(k).nodeSize;
	return { from, to, href: link.attrs.href };
}
