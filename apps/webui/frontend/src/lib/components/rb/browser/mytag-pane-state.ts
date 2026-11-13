/**
 * The My Tag pane's checkboxes for the current selection (issue #10026, rekordbox My Tag pane, mimicked).
 *
 * The tag catalog grouped by tag type, in type order, with untyped (`general`) tags and tags of an
 * unknown type under Other at the end. Each tag reads checked when every selected track holds it,
 * mixed when some do and clear when none. Pure: no Svelte, no DOM.
 */
import {
	GENERAL_TAG_TYPE,
	normalizeTagName,
	type TagCatalogEntry,
	type TagTypeRow
} from '$lib/rb/tag-display';

export type MyTagCheck = 'checked' | 'mixed' | 'clear';

export interface MyTagPaneTag {
	name: string;
	color: string | null;
	state: MyTagCheck;
	/** Selected tracks holding this tag. */
	held: number;
}

export interface MyTagPaneGroup {
	/** The tag type id, or `other` for untyped tags. */
	id: string;
	label: string;
	tags: MyTagPaneTag[];
}

/** The group id and label untyped tags go under. */
export const OTHER_GROUP = { id: 'other', label: 'Other' } as const;

/** One state per tag: how many of the selected tracks hold it decides checked, mixed or clear. */
export function myTagCheck(held: number, selected: number): MyTagCheck {
	if (selected <= 0 || held <= 0) return 'clear';
	return held >= selected ? 'checked' : 'mixed';
}

/** The pane's groups for a selection: every catalog tag, grouped by type, each with its checkbox state. */
export function myTagPaneGroups(
	catalog: readonly TagCatalogEntry[],
	types: readonly TagTypeRow[],
	assignments: Readonly<Record<string, readonly string[]>>,
	selectedIds: readonly string[]
): MyTagPaneGroup[] {
	const selected = [...new Set(selectedIds)];
	const heldBy = new Map<string, number>();
	for (const stableId of selected) {
		for (const norm of new Set((assignments[stableId] ?? []).map(normalizeTagName))) {
			heldBy.set(norm, (heldBy.get(norm) ?? 0) + 1);
		}
	}
	const typed = [...types]
		.filter((type) => type.id !== GENERAL_TAG_TYPE)
		.sort((a, b) => a.sort_index - b.sort_index || a.label.localeCompare(b.label));
	const groups = new Map<string, MyTagPaneGroup>(
		typed.map((type) => [type.id, { id: type.id, label: type.label, tags: [] }])
	);
	const other: MyTagPaneGroup = { ...OTHER_GROUP, tags: [] };
	const byName = [...catalog].sort((a, b) => a.name.localeCompare(b.name));
	for (const tag of byName) {
		const held = heldBy.get(tag.name_norm) ?? 0;
		const entry = {
			name: tag.name,
			color: tag.color,
			state: myTagCheck(held, selected.length),
			held
		};
		(groups.get(tag.type) ?? other).tags.push(entry);
	}
	return [...groups.values(), other].filter((group) => group.tags.length > 0);
}

/** The write a click makes: a checked tag comes off every selected track; mixed or clear goes on all of them. */
export function myTagToggleWrite(
	tag: Pick<MyTagPaneTag, 'name' | 'state'>,
	selectedIds: readonly string[]
): { stable_ids: string[]; add: string[]; remove: string[] } {
	const stableIds = [...new Set(selectedIds)];
	if (stableIds.length === 0)
		throw new Error('my tag pane: a toggle needs at least one selected track');
	return tag.state === 'checked'
		? { stable_ids: stableIds, add: [], remove: [tag.name] }
		: { stable_ids: stableIds, add: [tag.name], remove: [] };
}
