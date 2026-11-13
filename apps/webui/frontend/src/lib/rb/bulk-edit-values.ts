import { normalizeRating } from '$lib/rating';
import type { BulkEditPatch } from '$lib/rb/api-edit-suite';

export const MIXED_READOUT = 'Multiple';

export type FieldConsensus<T> = { kind: 'shared'; value: T } | { kind: 'mixed' };

export type BulkEditRowFields = {
	stable_id: string;
	rating: number | null;
	comments: string | null;
};

export type BulkEditConsensus = {
	rating: FieldConsensus<number | null>;
	notes: FieldConsensus<string>;
	comments: FieldConsensus<string>;
};

export function fieldConsensus<T>(values: readonly T[]): FieldConsensus<T> {
	if (values.length === 0) {
		throw new Error('fieldConsensus requires at least one value');
	}
	const first = values[0];
	for (let i = 1; i < values.length; i++) {
		if (!Object.is(values[i], first)) {
			return { kind: 'mixed' };
		}
	}
	return { kind: 'shared', value: first };
}

function _normalizeText(text: string | null | undefined): string {
	return text ?? '';
}

export function bulkEditFieldValues(
	stableIds: readonly string[],
	rows: readonly BulkEditRowFields[]
): BulkEditConsensus {
	if (stableIds.length === 0) {
		return {
			rating: { kind: 'shared', value: null },
			notes: { kind: 'shared', value: '' },
			comments: { kind: 'shared', value: '' }
		};
	}

	const rowById = new Map<string, BulkEditRowFields>();
	for (const row of rows) {
		if (!rowById.has(row.stable_id)) {
			rowById.set(row.stable_id, row);
		}
	}

	const ratings: (number | null)[] = [];
	const notes: string[] = [];
	const comments: string[] = [];
	for (const id of stableIds) {
		const row = rowById.get(id);
		ratings.push(normalizeRating(row?.rating ?? null));
		// Browser rows carry the track's notes as `comments` (rowFromListWire); they have no notes key.
		notes.push(_normalizeText(row?.comments));
		comments.push(_normalizeText(row?.comments));
	}

	return {
		rating: fieldConsensus(ratings),
		notes: fieldConsensus(notes),
		comments: fieldConsensus(comments)
	};
}

/** One field as the modal holds it: whether its Set box is ticked, and the text in its input. */
export type BulkEditFieldDraft = { set: boolean; text: string };

export type BulkEditDraft = {
	stableIds: string[];
	etags: Record<string, string>;
	rating: BulkEditFieldDraft;
	notes: BulkEditFieldDraft;
	genre: BulkEditFieldDraft;
	comments: BulkEditFieldDraft;
	tagsAdd: string;
	tagsRemove: string;
};

export type BulkEditPayloadResult =
	{ ok: true; patch: BulkEditPatch } | { ok: false; error: string };

function _splitTags(raw: string): string[] {
	return raw
		.split(',')
		.map((t) => t.trim())
		.filter((t) => t !== '');
}

/** The input text a field starts with: the shared value, or blank when mixed (never the readout). */
export function initialFieldText<T>(
	consensus: FieldConsensus<T>,
	show: (value: T) => string
): string {
	return consensus.kind === 'shared' ? show(consensus.value) : '';
}

/** The PATCH /bulk-edit body for a draft: only ticked fields are sent, so an untouched mixed field stays per-track. */
export function bulkEditPayload(draft: BulkEditDraft): BulkEditPayloadResult {
	const add = _splitTags(draft.tagsAdd);
	const remove = _splitTags(draft.tagsRemove);
	const anySet = draft.rating.set || draft.notes.set || draft.genre.set || draft.comments.set;
	if (!anySet && add.length === 0 && remove.length === 0) {
		return { ok: false, error: 'set at least one field to apply' };
	}
	const patch: BulkEditPatch = { stable_ids: draft.stableIds, expected_etags: draft.etags };
	if (draft.rating.set) {
		const trimmed = draft.rating.text.trim();
		const parsed = trimmed === '' ? Number.NaN : Number(trimmed);
		if (!Number.isFinite(parsed)) {
			return { ok: false, error: 'enter a valid rating to apply' };
		}
		patch.rating = parsed;
	}
	if (draft.notes.set) patch.notes = draft.notes.text;
	if (draft.genre.set) patch.genre = draft.genre.text;
	if (draft.comments.set) patch.comments = draft.comments.text;
	if (add.length > 0) patch.tags_add = add;
	if (remove.length > 0) patch.tags_remove = remove;
	return { ok: true, patch };
}

/** Whether a keydown is the apply chord: Ctrl+Enter (Cmd+Enter on macOS), no Shift or Alt. */
export function isApplyChord(event: {
	key: string;
	ctrlKey: boolean;
	metaKey: boolean;
	shiftKey: boolean;
	altKey: boolean;
}): boolean {
	return (
		event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.shiftKey && !event.altKey
	);
}
