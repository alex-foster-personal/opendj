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

import { API_BASE } from '$lib/api';
import { parseVocals, type Vocals } from './api-rb';

interface RowAssetWire {
	preview_b64: string | null;
	preview_max: number | null;
	vocals: unknown;
	artwork_available: boolean | null;
	artwork_status: 'ok' | 'no_image_path' | 'unresolved' | 'file_missing';
}

/** The row fields an answer settles (preview-strip-fill's RowAssetTarget). */
interface Target {
	stable_id: string;
	vocals: Vocals;
	artwork_available: boolean | null;
	artwork_status: RowAssetWire['artwork_status'];
}

/** Ask for `ids`, settle every row among `rows` that has one of them, and return
 * the strips in the filler's shape (null: nothing on disk, or not a track). */
export async function fetchAndApplyRowAssets(
	ids: string[],
	rows: readonly Target[]
): Promise<{ strips: Record<string, { preview_b64: string; preview_max: number } | null>; pending: string[] }> {
	const response = await fetch(`${API_BASE}/api/v1/library/row-assets`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify({ ids })
	});
	if (!response.ok) throw new Error(`POST /library/row-assets failed with ${response.status}`);
	const answer = (await response.json()) as { assets: Record<string, RowAssetWire>; pending: string[] };
	if (typeof answer.assets !== 'object' || answer.assets === null || !Array.isArray(answer.pending)) {
		throw new Error('POST /library/row-assets answered without assets or pending');
	}
	const wanted = new Set(ids);
	for (const row of rows) {
		if (!wanted.has(row.stable_id)) continue;
		const asset = answer.assets[row.stable_id];
		if (asset === undefined) continue;
		row.vocals = parseVocals(asset.vocals);
		row.artwork_available = asset.artwork_available;
		row.artwork_status = asset.artwork_status;
	}
	const strips: Record<string, { preview_b64: string; preview_max: number } | null> = {};
	for (const id of ids) {
		const asset = answer.assets[id];
		strips[id] =
			asset === undefined || asset.preview_b64 === null || asset.preview_max === null
				? null
				: { preview_b64: asset.preview_b64, preview_max: asset.preview_max };
	}
	return { strips, pending: answer.pending };
}
