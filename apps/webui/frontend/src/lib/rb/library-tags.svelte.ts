/**
 * The tag state the track table reads: which tags each track holds, the catalog
 * and the types. Tags are not on the library index row, so they load here once
 * and refresh on `library.changed` kind `mytags`; the index stays as cheap as it was.
 */
import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
import { pushToast } from '$lib/stores.svelte';
import {
	assignTags,
	fetchTagAssignments,
	fetchTagCatalog,
	fetchTagTypes,
	patchTagType,
	undoLastLibraryChange
} from './api-edit-suite';
import { RbApiError } from './api-rb-error';
import {
	TAG_REMOVE_SNOOZE_MS,
	tagRemoveSnoozed,
	tagTypeColumnsOf,
	tagsCellNames,
	typeColumnNames,
	undoToastMessage,
	type TagCatalogEntry,
	type TagTypeColumn,
	type TagTypeRow
} from './tag-display';

const SNOOZE_KEY = 'mdt.rb.tag-remove-snooze-until';

export interface TagEditorTarget {
	stableIds: string[];
	typeId: string | null;
	x: number;
	y: number;
}

export interface TagRemovalRequest {
	stableId: string;
	name: string;
}

export const tagState = $state({
	catalog: [] as TagCatalogEntry[],
	types: [] as TagTypeRow[],
	assignments: {} as Record<string, string[]>,
	editor: null as TagEditorTarget | null,
	removal: null as TagRemovalRequest | null
});

const catalogByNorm = $derived(new Map(tagState.catalog.map((tag) => [tag.name_norm, tag])));
const typeColumns = $derived(tagTypeColumnsOf(tagState.types));

let started = false;

export async function refreshTags(): Promise<void> {
	const [catalog, types, assignments] = await Promise.all([
		fetchTagCatalog(),
		fetchTagTypes(),
		fetchTagAssignments()
	]);
	tagState.catalog = catalog;
	tagState.types = types;
	tagState.assignments = assignments;
}

export function reportTagFailure(message: string): void {
	pushToast(message, 'error');
}

function _refreshLoudly(): void {
	refreshTags().catch((exc: unknown) => pushToast(`tags did not load: ${String(exc)}`, 'error'));
}

/** Revert the newest tag change. The ⌘Z router decides when this is the one to run. */
export async function undoNewestTagChange(): Promise<void> {
	try {
		const undone = await undoLastLibraryChange();
		pushToast(undoToastMessage(undone.op), 'info');
		await refreshTags();
	} catch (exc) {
		if (exc instanceof RbApiError && exc.code === 'NOT_FOUND') {
			pushToast('Nothing to undo', 'info');
		} else {
			// A refused undo stays on the stack; the server says why (R-UNDO-2).
			pushToast(exc instanceof RbApiError ? exc.message : `undo failed: ${String(exc)}`, 'error');
		}
	}
}

/** Load once for the page and keep following changes. Safe to call repeatedly. */
export function ensureTagsLoaded(): void {
	if (started) return;
	started = true;
	subscribeKind('mytags', _refreshLoudly);
	subscribeResync(_refreshLoudly);
	_refreshLoudly();
}

export function tagTypeColumns(): TagTypeColumn[] {
	return typeColumns;
}

export function tagsCellFor(stableId: string): string[] {
	return tagsCellNames(tagState.assignments[stableId] ?? [], catalogByNorm, tagState.types);
}

export function typeColumnFor(stableId: string, typeId: string): string[] {
	return typeColumnNames(tagState.assignments[stableId] ?? [], typeId, catalogByNorm);
}

export function openTagEditor(target: TagEditorTarget): void {
	tagState.removal = null;
	tagState.editor = target;
}

function _snoozeUntil(): number | null {
	const raw = window.localStorage.getItem(SNOOZE_KEY);
	return raw === null ? null : Number(raw);
}

export function snoozeTagRemovalConfirm(now: number): void {
	window.localStorage.setItem(SNOOZE_KEY, String(now + TAG_REMOVE_SNOOZE_MS));
}

export async function applyTagChange(body: Parameters<typeof assignTags>[0]): Promise<void> {
	const write = await assignTags(body);
	if (write.undo_id !== null) await refreshTags();
}

export async function removeTagNow(request: TagRemovalRequest): Promise<void> {
	try {
		await applyTagChange({ stable_ids: [request.stableId], remove: [request.name] });
		pushToast(`Removed #${request.name}. ⌘Z undoes it.`, 'info');
	} catch (exc) {
		pushToast(`tag not removed: ${String(exc)}`, 'error');
	}
}

/** The × on a tag: confirm unless the hour's snooze is running (D13). */
export function requestTagRemoval(request: TagRemovalRequest): void {
	if (tagRemoveSnoozed(_snoozeUntil(), Date.now())) void removeTagNow(request);
	else tagState.removal = request;
}

export interface TagTypeChooserEntry {
	id: string;
	label: string;
	column: boolean;
	inTags: boolean;
}

export function tagTypeChooserEntries(): TagTypeChooserEntry[] {
	return tagState.types
		.filter((type) => type.id !== 'general')
		.map((type) => ({
			id: type.id,
			label: type.label,
			column: type.show_as_column === 1,
			inTags: type.show_in_tags === 1
		}));
}

async function _patchType(id: string, changes: Parameters<typeof patchTagType>[1]): Promise<void> {
	try {
		await patchTagType(id, changes);
		await refreshTags();
	} catch (exc) {
		pushToast(`tag type not changed: ${String(exc)}`, 'error');
	}
}

export function toggleTagTypeColumn(entry: TagTypeChooserEntry): void {
	void _patchType(entry.id, { show_as_column: !entry.column });
}

export function toggleTagTypeInTags(entry: TagTypeChooserEntry): void {
	void _patchType(entry.id, { show_in_tags: !entry.inTags });
}
