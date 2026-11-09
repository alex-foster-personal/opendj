/**
 * SETNOTE-6340d/05/09: following and editing links from the editor. A
 * Cmd/Ctrl-click follows a link (a plain click places the caret, so the note
 * edits like text); a right-click opens the link menu; hovering reports the
 * link under the pointer so the pad can offer hover + 1-4. What a click means
 * is decided by `clickTarget`, on editor state alone.
 */
import { Plugin, type EditorState } from 'prosemirror-state';
import type { Node as PMNode } from 'prosemirror-model';
import { linkAt } from './commands';
import { noteSchema, parseHref, refTarget, type NoteTarget } from './schema';

export type LinkPluginHandlers = {
	follow: (target: NoteTarget) => void;
	contextMenu: (event: MouseEvent) => void;
	hover: (target: NoteTarget | null) => void;
};

/** What a click follows: nothing without Cmd/Ctrl, else the clicked chip or the text link at `pos`. */
export function clickTarget(
	state: Pick<EditorState, 'doc'>,
	pos: number,
	chip: PMNode | null,
	mod: boolean
): NoteTarget | null {
	if (!mod) return null;
	if (chip) return chip.type === noteSchema.nodes.track_ref ? refTarget(chip) : null;
	return parseHref(linkAt(state, pos)?.href ?? '');
}

/** What the right-click menu acts on at `pos` (SETNOTE-10457): a `track_ref` chip right
 * after `pos`, else the text link there (with its range, so Remove link can unlink it),
 * else a chip right before `pos`. A chip and a text link to the same track give the same
 * `NoteTarget`, so the menu offers the same Open / load action for both. */
export function menuTargetAt(
	state: Pick<EditorState, 'doc'>,
	pos: number
): { target: NoteTarget; link: ReturnType<typeof linkAt> } | null {
	const $pos = state.doc.resolve(pos);
	const isChip = (node: PMNode | null | undefined): node is PMNode =>
		node?.type === noteSchema.nodes.track_ref;
	if (isChip($pos.nodeAfter)) return { target: refTarget($pos.nodeAfter), link: null };
	const link = linkAt(state, pos);
	const linked = link === null ? null : parseHref(link.href);
	if (link !== null && linked !== null) return { target: linked, link };
	if (isChip($pos.nodeBefore)) return { target: refTarget($pos.nodeBefore), link: null };
	return null;
}

/** The link a rendered element belongs to, read from the attributes the schema writes. */
export function targetOfElement(el: Element | null): NoteTarget | null {
	const hit = el?.closest('[data-href], [data-stable-id]');
	if (!hit) return null;
	const href = hit.getAttribute('data-href');
	if (href !== null) return parseHref(href);
	const stableId = hit.getAttribute('data-stable-id')!;
	const stem = hit.getAttribute('data-stem');
	return stem ? { kind: 'stem', stableId, stem } : { kind: 'track', stableId };
}

export function linkPlugin({ follow, contextMenu, hover }: LinkPluginHandlers): Plugin {
	const followIf = (target: NoteTarget | null): boolean => {
		if (target) follow(target);
		return target !== null;
	};
	const mod = (event: MouseEvent): boolean => event.metaKey || event.ctrlKey;
	return new Plugin({
		props: {
			handleClickOn: (view, pos, node, _nodePos, event, direct) =>
				direct && node.isAtom ? followIf(clickTarget(view.state, pos, node, mod(event))) : false,
			handleClick: (view, pos, event) => followIf(clickTarget(view.state, pos, null, mod(event))),
			handleDOMEvents: {
				contextmenu: (_view, event) => {
					contextMenu(event);
					return true;
				},
				pointermove: (_view, event) => {
					hover(targetOfElement(event.target as Element));
					return false;
				},
				pointerleave: () => {
					hover(null);
					return false;
				}
			}
		}
	});
}
