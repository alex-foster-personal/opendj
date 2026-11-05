/**
 * Tag rules with no state and no network (Library Organization PRD R-TAG,
 * R-TT), so node --test drives them directly. Types are data: nothing here
 * names `instrument` or `mood`.
 */
import type { components } from '../api-types';

export type TagTypeRow = components['schemas']['TagTypeOut'];
export type TagCatalogEntry = components['schemas']['TagSummaryOut'];

export interface TagTypeColumn {
	id: string;
	label: string;
	singular: string;
	width: number;
}

export const GENERAL_TAG_TYPE = 'general';
export const TAG_REMOVE_SNOOZE_MS = 60 * 60 * 1000;
const DEFAULT_TYPE_COLUMN_WIDTH = 120;

/** The server's tag identity: NFKC, casefolded, trimmed. */
export function normalizeTagName(name: string): string {
	return name.normalize('NFKC').toLocaleLowerCase('und').trim();
}

/** The types with their own column, in type order. General is the Tags column. */
export function tagTypeColumnsOf(types: readonly TagTypeRow[]): TagTypeColumn[] {
	return types
		.filter((type) => type.id !== GENERAL_TAG_TYPE && type.show_as_column === 1)
		.sort((a, b) => a.sort_index - b.sort_index)
		.map((type) => ({
			id: type.id,
			label: type.label,
			singular: type.singular,
			width: type.column_width ?? DEFAULT_TYPE_COLUMN_WIDTH
		}));
}

function typeOf(tag: string, catalog: ReadonlyMap<string, TagCatalogEntry>): string {
	return catalog.get(normalizeTagName(tag))?.type ?? GENERAL_TAG_TYPE;
}

/** What the Tags cell shows. A type with no column of its own stays here even
 * when "also in Tags" is off, so turning a column off never hides a tag (R-TT-2). */
export function tagsCellNames(
	tags: readonly string[],
	catalog: ReadonlyMap<string, TagCatalogEntry>,
	types: readonly TagTypeRow[]
): string[] {
	const byId = new Map(types.map((type) => [type.id, type]));
	return tags.filter((tag) => {
		const type = byId.get(typeOf(tag, catalog));
		return type === undefined || type.show_in_tags === 1 || type.show_as_column === 0;
	});
}

export function typeColumnNames(
	tags: readonly string[],
	typeId: string,
	catalog: ReadonlyMap<string, TagCatalogEntry>
): string[] {
	return tags.filter((tag) => typeOf(tag, catalog) === typeId);
}

/** The tag groups of the Taglists tab: types in order, empty ones left out. */
export function groupTagsByType(
	catalog: readonly TagCatalogEntry[],
	types: readonly TagTypeRow[]
): { type: TagTypeRow; tags: TagCatalogEntry[] }[] {
	return [...types]
		.sort((a, b) => a.sort_index - b.sort_index)
		.map((type) => ({ type, tags: catalog.filter((tag) => tag.type === type.id) }))
		.filter((group) => group.tags.length > 0);
}

export function tagRemoveSnoozed(snoozeUntil: number | null, now: number): boolean {
	return snoozeUntil !== null && now < snoozeUntil;
}

const UNDO_LABELS: Readonly<Record<string, string>> = {
	'tag.assign': 'tag change',
	'tag.rename': 'tag rename',
	'tag.delete': 'tag delete',
	'tag.create': 'new tag',
	'tag.retype': 'tag type change'
};

/** The toast after an undo. It names the action (R-UNDO-2). */
export function undoToastMessage(op: string): string {
	return `Undid ${UNDO_LABELS[op] ?? op}`;
}

/** One ⌘Z reverts one change, the newest of the two undo stacks (R-UNDO-1).
 * Tags have their own journal; playlist edits keep their history. */
export function undoTarget(
	tagWriteAt: number | null,
	playlistEditAt: number | null
): 'tag' | 'playlist' {
	if (tagWriteAt === null) return 'playlist';
	return playlistEditAt !== null && playlistEditAt > tagWriteAt ? 'playlist' : 'tag';
}
