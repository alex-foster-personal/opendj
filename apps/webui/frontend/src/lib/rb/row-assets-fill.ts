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
 * Loaded with a dynamic import on the first batch, so none of it rides the
 * /performance route's eager bundle.
 */

import { api, unwrap } from '$lib/api/client';
import { parseVocals, type Vocals } from './api-rb';
import type { PreviewStripBatch } from './preview-strip-fill';

/** The row fields a row-assets answer settles. */
export interface RowAssetTarget {
	stable_id: string;
	vocals: Vocals;
	artwork_available: boolean | null;
	artwork_status: 'ok' | 'no_image_path' | 'unresolved' | 'file_missing';
}

/** Ask for `ids`, settle every row among `rows` that has one of them, and return
 * the strips in the filler's shape (null: nothing on disk, or not a track). */
export async function fetchAndApplyRowAssets(
	ids: string[],
	rows: readonly RowAssetTarget[]
): Promise<PreviewStripBatch> {
	const answer = await unwrap(api.POST('/api/v1/library/row-assets', { body: { ids } }));
	const wanted = new Set(ids);
	for (const row of rows) {
		if (!wanted.has(row.stable_id)) continue;
		const asset = answer.assets[row.stable_id];
		if (asset === undefined) continue;
		row.vocals = parseVocals(asset.vocals);
		row.artwork_available = asset.artwork_available;
		row.artwork_status = asset.artwork_status;
	}
	const strips: PreviewStripBatch['strips'] = {};
	for (const id of ids) {
		const asset = answer.assets[id];
		strips[id] =
			asset === undefined || asset.preview_b64 === null || asset.preview_max === null
				? null
				: { preview_b64: asset.preview_b64, preview_max: asset.preview_max };
	}
	return { strips, pending: answer.pending };
}
