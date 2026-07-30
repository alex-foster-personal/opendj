/**
 * Pure display helpers for agent-proposed dirty-metadata fixes.
 * TrackTable paints `proposed` orange and `approved` with a RHS green tick.
 */

export type EnrichmentStatus = 'proposed' | 'approved';

export interface EnrichmentField {
	value: string;
	status: EnrichmentStatus;
}

/** Per-row enrichment bag (optional on BrowserRow; BE attaches later). */
export interface RowEnrichment {
	title?: EnrichmentField;
	artist?: EnrichmentField;
}

export type EnrichCellMode = 'raw' | 'proposed' | 'approved';

export interface EnrichCellState {
	/** Text shown in the cell (proposal value or raw). */
	text: string;
	mode: EnrichCellMode;
}

/** Resolve title/artist cell text + visual mode from raw + optional field. */
export function enrichmentCellState(
	raw: string | null | undefined,
	field: EnrichmentField | null | undefined
): EnrichCellState {
	if (field !== null && field !== undefined && field.value !== '') {
		if (field.status === 'proposed') {
			return { text: field.value, mode: 'proposed' };
		}
		if (field.status === 'approved') {
			return { text: field.value, mode: 'approved' };
		}
	}
	return { text: raw ?? '', mode: 'raw' };
}

/** Display string for the title cell (proposal overrides raw when present). */
export function displayTitle(row: {
	title: string | null;
	enrichment?: RowEnrichment | null;
}): string {
	return enrichmentCellState(row.title, row.enrichment?.title).text;
}

/** Display string for the artist cell. */
export function displayArtist(row: {
	artist: string | null;
	enrichment?: RowEnrichment | null;
}): string {
	return enrichmentCellState(row.artist, row.enrichment?.artist).text;
}
