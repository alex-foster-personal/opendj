export const MIXED_READOUT = 'Multiple';

export type FieldConsensus<T> = { kind: 'shared'; value: T } | { kind: 'mixed' };

export type BulkEditRowFields = {
	stable_id: string;
	rating: number | null;
	comments: string | null;
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

function _normalizeNotes(comments: string | null | undefined): string {
	return comments ?? '';
}

export function bulkEditFieldValues(
	stableIds: readonly string[],
	rows: readonly BulkEditRowFields[]
): { rating: FieldConsensus<number | null>; notes: FieldConsensus<string> } {
	if (stableIds.length === 0) {
		return {
			rating: { kind: 'shared', value: null },
			notes: { kind: 'shared', value: '' }
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
	for (const id of stableIds) {
		const row = rowById.get(id);
		ratings.push(row?.rating ?? null);
		notes.push(_normalizeNotes(row?.comments));
	}

	return {
		rating: fieldConsensus(ratings),
		notes: fieldConsensus(notes)
	};
}
