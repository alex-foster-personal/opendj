/**
 * SETNOTE-6340f: `@query` typed at the start of a block or after whitespace
 * opens the track search. The plugin only reports where the query is; the
 * popover (in Svelte) searches, draws and picks. While it is open the
 * popover gets first refusal on ArrowUp/ArrowDown/Enter/Tab, and Escape
 * dismisses it until that `@` is deleted. An `@` already inside a link (a
 * picked mention) never reopens it, though queries may contain spaces.
 */
import { Plugin, type EditorState } from 'prosemirror-state';
import type { EditorView } from 'prosemirror-view';
import { noteSchema } from './schema';

export interface Mention {
	query: string;
	from: number;
	to: number;
	left: number;
	bottom: number;
}

const MENTION_RE = /(?:^|\s)@([^@\s][^@\n]{0,39})$/;
const POPOVER_KEYS = new Set(['ArrowUp', 'ArrowDown', 'Enter', 'Tab']);

export function findMention(state: EditorState): Pick<Mention, 'query' | 'from' | 'to'> | null {
	const { selection } = state;
	const $from = selection.$from;
	if (!selection.empty || !$from.parent.isTextblock) return null;
	const m = MENTION_RE.exec($from.parent.textBetween(0, $from.parentOffset, undefined, '\ufffc'));
	if (!m) return null;
	const from = $from.pos - m[1].length - 1;
	return state.doc.rangeHasMark(from, from + 1, noteSchema.marks.link) ? null : { query: m[1], from, to: $from.pos };
}

export function mentionPlugin(on: {
	change: (mention: Mention | null) => void;
	key: (event: KeyboardEvent) => boolean;
}): Plugin {
	let current: Mention | null = null;
	let dismissedFrom: number | null = null;
	const set = (next: Mention | null): void => {
		if (next?.query === current?.query && next?.from === current?.from) return;
		current = next;
		on.change(next);
	};
	const report = (view: EditorView): void => {
		const found = findMention(view.state);
		if (found === null) dismissedFrom = null;
		if (found === null || found.from === dismissedFrom) return set(null);
		const { left, bottom } = view.coordsAtPos(found.from);
		set({ ...found, left, bottom });
	};
	return new Plugin({
		view: (view) => {
			report(view);
			return { update: report, destroy: () => set(null) };
		},
		props: {
			handleKeyDown(_view, event) {
				if (current === null) return false;
				if (event.key === 'Escape') {
					dismissedFrom = current.from;
					set(null);
					return true;
				}
				return POPOVER_KEYS.has(event.key) && on.key(event);
			}
		}
	});
}
