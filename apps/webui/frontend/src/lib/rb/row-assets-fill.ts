/**
 * The per-row disk reads a library index row leaves out, for rows in view (LIBM-172).
 *
 * The index (`GET /tracks/index`) carries no preview strip, vocal regions or cover
 * verdict: reading those off disk for every row was 7.8 s of a cold 9,713-row
 * index. TrackTable's PreviewStripFiller already asks for strip-less rows in view
 * (plus one screen of margin), debounced and batched; its batch now goes to
 * `POST /library/row-assets`, which answers all three. This module writes the
 * vocals and cover verdict into the matching rows and hands the strips back to the
 * filler, which owns retries for `pending` ids.
 *
 * Loaded with a dynamic import on the first batch (preview-strip-fill's
 * `fetchRowAssetsLazily`), so none of it rides the /performance route's eager bundle.
 */

import { fetchRowAssets, parseVocals, type Vocals } from './api-rb';

type ArtworkStatus = 'ok' | 'no_image_path' | 'unresolved' | 'file_missing';

/** The row fields a `POST /library/row-assets` answer settles (LIBM-172). */
export interface RowAssetTarget {
	stable_id: string;
	vocals: Vocals;
	artwork_available: boolean | null;
	artwork_status: ArtworkStatus;
}

/** Ask for `ids`, settle every row among `rows` that has one of them, and return
 * the strips in the filler's shape (null: nothing on disk, or not a track). */
export async function fetchAndApplyRowAssets(
	ids: string[],
	rows: readonly RowAssetTarget[]
): Promise<{ strips: Record<string, { preview_b64: string; preview_max: number } | null>; pending: string[] }> {
	const answer = await fetchRowAssets(ids);
	if (typeof answer.assets !== 'object' || answer.assets === null || !Array.isArray(answer.pending)) {
		throw new Error('POST /library/row-assets answered without assets or pending');
	}
	const wanted = new Set(ids);
	for (const row of rows) {
		if (!wanted.has(row.stable_id)) continue;
		const asset = answer.assets[row.stable_id];
		if (asset === undefined) continue;
		row.vocals = parseVocals(asset.vocals);
		row.artwork_available = asset.artwork_available ?? null;
		row.artwork_status = asset.artwork_status as ArtworkStatus;
	}
	const strips: Record<string, { preview_b64: string; preview_max: number } | null> = {};
	for (const id of ids) {
		const asset = answer.assets[id];
		const b64 = asset?.preview_b64 ?? null;
		const max = asset?.preview_max ?? null;
		strips[id] = b64 === null || max === null ? null : { preview_b64: b64, preview_max: max };
	}
	return { strips, pending: answer.pending };
}
