/**
 * Editing behaviour for the Set notes editor: markdown-style input rules
 * (`# ` heading, `- ` / `* ` bullet, `**bold**`), list keys and history.
 * Plugins passed in run first, so the mention popover sees its keys before
 * Enter splits a list item.
 */
import { baseKeymap, chainCommands, toggleMark } from 'prosemirror-commands';
import { history, redo, undo } from 'prosemirror-history';
import {
	InputRule,
	inputRules,
	textblockTypeInputRule,
	undoInputRule,
	wrappingInputRule
} from 'prosemirror-inputrules';
import { keymap } from 'prosemirror-keymap';
import { liftListItem, splitListItem } from 'prosemirror-schema-list';
import { TextSelection, type Command, type Plugin } from 'prosemirror-state';
import { noteSchema } from './schema';

const { nodes, marks } = noteSchema;

const boldRule = new InputRule(/\*\*([^*\s](?:[^*]*[^*\s])?)\*\*$/, (state, match, start, end) =>
	state.tr
		.replaceWith(start, end, noteSchema.text(match[1], [marks.strong.create()]))
		.removeStoredMark(marks.strong)
);

const liftAtItemStart: Command = (state, dispatch) => {
	const { selection } = state;
	if (!(selection instanceof TextSelection) || !selection.$cursor) return false;
	const $cursor = selection.$cursor;
	if ($cursor.parentOffset > 0 || $cursor.node(-1).type !== nodes.list_item) return false;
	return liftListItem(nodes.list_item)(state, dispatch);
};

export function editorPlugins(first: Plugin[]): Plugin[] {
	return [
		...first,
		history(),
		inputRules({
			rules: [
				textblockTypeInputRule(/^(#{1,3})\s$/, nodes.heading, (m) => ({ level: m[1].length })),
				wrappingInputRule(/^\s*[-*]\s$/, nodes.bullet_list),
				boldRule
			]
		}),
		keymap({
			'Mod-b': toggleMark(marks.strong),
			Enter: splitListItem(nodes.list_item),
			'Shift-Tab': liftListItem(nodes.list_item),
			Backspace: chainCommands(undoInputRule, liftAtItemStart),
			'Mod-z': undo,
			'Mod-Shift-z': redo,
			'Mod-y': redo
		}),
		keymap(baseKeymap)
	];
}
