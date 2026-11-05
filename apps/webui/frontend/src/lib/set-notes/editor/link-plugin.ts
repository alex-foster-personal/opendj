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
