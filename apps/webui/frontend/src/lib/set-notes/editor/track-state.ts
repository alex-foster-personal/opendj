/**
 * SETNOTE-6340d: chips and track links whose track is not in this library render
 * struck and inert, and `problem` says why for the pad's ribbon. Existence is
 * checked once per id per pad session and the editor redraws as answers arrive.
 *
 * No `title` goes on the editor's DOM: the shell's hover tooltip parks titles
 * on hover, and ProseMirror redraws any node whose attributes change under it,
 * so a titled link flickers and swallows the pointer moves hover + 1-4 needs.
 */
import type { Node as PMNode } from 'prosemirror-model';
import { Plugin, PluginKey } from 'prosemirror-state';
import { Decoration, DecorationSet, type EditorView } from 'prosemirror-view';
import { nodeTarget, type NoteTarget } from './schema';

/** The deck a followed track link loads onto while /performance is mounted. */
export const FIRST_DECK = 1;

/** The link under the pointer, and why it cannot be followed if it cannot. */
export type HoveredLink = { target: NoteTarget; problem: string | null };

type Known = 'pending' | 'ok' | 'missing' | 'error';

const redraw = new PluginKey('set-notes-track-state');

export function createTrackState(exists: (stableId: string) => Promise<boolean>): {
	plugin: Plugin;
	usable: (target: NoteTarget) => boolean;
	problem: (target: NoteTarget) => string | null;
} {
	const known = new Map<string, Known>();
	let view: EditorView | null = null;
	const stateOf = (target: NoteTarget): Known | undefined => {
		if (target.kind === 'http') return 'ok';
		const id = target.stableId;
		if (!known.has(id)) {
			known.set(id, 'pending');
			exists(id)
				.then((ok) => known.set(id, ok ? 'ok' : 'missing'))
				.catch(() => known.set(id, 'error'))
				.finally(() => view?.dispatch(view.state.tr.setMeta(redraw, true)));
		}
		return known.get(id);
	};
	const problem = (target: NoteTarget): string | null => {
		if (target.kind === 'http') return null;
		const state = stateOf(target);
		if (state === 'missing') return `No track ${target.stableId} in this library`;
		return state === 'error' ? `Could not check track ${target.stableId}` : null;
	};
	const decorate = (node: PMNode, pos: number): Decoration | null => {
		const target = nodeTarget(node);
		if (!target || problem(target) === null) return null;
		const attrs = { class: 'sn-missing' };
		return node.isText ? Decoration.inline(pos, pos + node.nodeSize, attrs) : Decoration.node(pos, pos + node.nodeSize, attrs);
	};
	const plugin = new Plugin({
		key: redraw,
		view: (v) => {
			view = v;
			return { destroy: () => (view = null) };
		},
		props: {
			decorations(state) {
				const found: Decoration[] = [];
				state.doc.descendants((node, pos) => {
					const deco = node.isInline ? decorate(node, pos) : null;
					if (deco) found.push(deco);
				});
				return DecorationSet.create(state.doc, found);
			}
		}
	});
	const usable = (target: NoteTarget): boolean => stateOf(target) === 'ok';
	return { plugin, usable, problem };
}
