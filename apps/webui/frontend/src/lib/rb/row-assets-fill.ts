/**
 * The per-row disk reads a library index row leaves out, for rows in view (LIBM-172).
 *
 * The index (`GET /tracks/index`) carries no preview strip, vocal regions, cover
 * verdict or stem bundle summary: reading those off disk for every row was 9 s of a
 * cold 9,713-row index. TrackTable's PreviewStripFiller already asks for strip-less
 * rows in view (plus one screen of margin), debounced and batched; its batch now
 * goes to `POST /library/row-assets`, which answers all four. This module writes
 * vocals, cover verdict and stems into the matching rows (as whole-row patches) and hands the strips back
 * to the filler, which owns retries for `pending` ids.
 *
 * Loaded with a dynamic import on the first batch (preview-strip-fill's
 * `fetchRowAssetsLazily`), so none of it rides the /performance route's eager bundle.
 */

import {
	decodePreviewStrip,
	fetchRowAssets,
	parseStemSummary,
	parseVocals,
	type PreviewStripData,
	type StemSummary,
	type Vocals
} from './api-rb';

import type { TempoCurveData } from '$lib/components/rb/wave/tempo-marks';

export type { PreviewStripData, Vocals };

type ArtworkStatus = 'ok' | 'no_image_path' | 'unresolved' | 'file_missing';

/** The row fields a `POST /library/row-assets` answer settles (LIBM-172). */
export interface RowAssetTarget {
	stable_id: string;
	vocals: Vocals;
	artwork_available: boolean | null;
	artwork_status: ArtworkStatus;
	stems: StemSummary | null;
	/** Row strip tempo curve (issue #7879), as the daemon stored it. */
	tempo_curve?: TempoCurveData | null;
}

type RowAssetsAnswer = Awaited<ReturnType<typeof fetchRowAssets>>;
type StripWire = { preview_b64: string; preview_max: number } | null;

/** Ask for `ids`, then settle them through `applyRowAssetsAnswer`. */
export async function fetchAndApplyRowAssets<T extends RowAssetTarget>(
	ids: string[],
	rows: () => readonly T[],
	settled: WeakSet<object>,
	patchRow: (stable_id: string, map: (row: T) => T) => void
): Promise<{ strips: Record<string, StripWire>; pending: string[] }> {
	return applyRowAssetsAnswer(ids, await fetchRowAssets(ids), rows, settled, patchRow);
}

/** Settle every row among `rows()` that has one of `ids` and return the strips in
 * the filler's shape (null: nothing on disk, or not a track). Each such row object
 * not still pending goes into `settled`, so the table can tell it from a refreshed
 * row that has none yet. The answer is published
 * through `patchRow` as whole-row replacements, never written into a row. */
export function applyRowAssetsAnswer<T extends RowAssetTarget>(
	ids: string[],
	answer: RowAssetsAnswer,
	rows: () => readonly T[],
	settled: WeakSet<object>,
	patchRow: (stable_id: string, map: (row: T) => T) => void
): { strips: Record<string, StripWire>; pending: string[] } {
	if (typeof answer.assets !== 'object' || answer.assets === null || !Array.isArray(answer.pending)) {
		throw new Error('POST /library/row-assets answered without assets or pending');
	}
	const wanted = new Set(ids);
	const pending = new Set(answer.pending);
	for (const row of rows()) {
		if (!wanted.has(row.stable_id)) continue;
		const settles = !pending.has(row.stable_id);
		const asset = answer.assets[row.stable_id];
		if (asset === undefined) {
			if (settles) settled.add(row);
			continue;
		}
		const fields = {
			vocals: parseVocals(asset.vocals),
			artwork_available: asset.artwork_available ?? null,
			artwork_status: asset.artwork_status as ArtworkStatus,
			stems: parseStemSummary(asset.stems),
			tempo_curve: asset.tempo ?? null
		};
		// Rows are raw (PR #12724): a field written in place never redraws, so the
		// answer publishes a whole new row, and that new object is the settled one.
		patchRow(row.stable_id, (held) => {
			const next = { ...held, ...fields };
			if (settles) settled.add(next);
			return next;
		});
	}
	const strips: Record<string, StripWire> = {};
	for (const id of ids) {
		const asset = answer.assets[id];
		const b64 = asset?.preview_b64 ?? null;
		const max = asset?.preview_max ?? null;
		strips[id] = b64 === null || max === null ? null : { preview_b64: b64, preview_max: max };
	}
	return { strips, pending: answer.pending };
}

function pendingList(value: unknown): readonly unknown[] {
	if (!Array.isArray(value)) throw new Error('row assets answered without a pending list: the response contract changed');
	return value;
}

/** One track's decoded strip and vocals, the same answer a library row in view
 * gets (CMDK-07, the Cmd K preview). null when the id is not a library track; a
 * contract breach in either half throws, so it is shown as one, never drawn as
 * "no data". Lives here rather than in its caller so the bar adds no importer
 * to api-rb, the most-imported module. */
export async function fetchPreviewAsset(
	stable_id: string
): Promise<{ strip: PreviewStripData | null; vocals: Vocals; pending: boolean } | null> {
	const answer = await fetchRowAssets([stable_id]);
	if (typeof answer.assets !== 'object' || answer.assets === null || !Array.isArray(answer.pending)) {
		throw new Error('POST /library/row-assets answered without assets or pending');
	}
	const asset = answer.assets[stable_id];
	if (asset === undefined) return null;
	return {
		strip: decodePreviewStrip(asset.preview_b64, asset.preview_max),
		vocals: parseVocals(asset.vocals),
		// Still being analyzed: the caller asks again rather than holding "no strip" for good.
		// A missing or non-list `pending` is contract drift, so it throws rather than reading as "done" (Sol P1, #6687).
		pending: pendingList(answer.pending).includes(stable_id)
	};
}
