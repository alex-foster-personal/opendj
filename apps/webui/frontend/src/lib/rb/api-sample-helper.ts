/**
 * Typed client for /api/v1/sample-helper (YouTube paste -> ingest staging).
 *
 * Same conventions as api-ingest.ts: relative API_BASE, fail-fast RbApiError.
 */

import { API_BASE } from '$lib/api';
import type { UploadFileResult } from '$lib/rb/api-ingest';
import { RbApiError } from './api-rb-error';

export type SampleDownloadOut = {
	batch: string;
	dest_dir: string;
	video_id: string;
	source_url: string;
	results: UploadFileResult[];
	preview_url: string | null;
};

async function _err(r: Response): Promise<never> {
	const body = (await r.json()) as { detail?: { code?: string; message?: string } | string };
	const detail = typeof body.detail === 'object' && body.detail !== null ? body.detail : undefined;
	const msg = typeof body.detail === 'string' ? body.detail : detail?.message;
	throw new RbApiError(r.status, detail?.code ?? `HTTP_${r.status}`, msg ?? r.statusText);
}

export async function downloadSampleFromUrl(
	url: string,
	batch: string,
	force = false
): Promise<SampleDownloadOut> {
	const r = await fetch(`${API_BASE}/api/v1/sample-helper/download`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ url, batch, force })
	});
	if (!r.ok) await _err(r);
	return (await r.json()) as SampleDownloadOut;
}

/** Staged ingest file for in-modal playback (served by the engine). */
export function samplePreviewSrc(previewUrl: string | null): string | null {
	if (previewUrl === null) return null;
	if (previewUrl.startsWith('http://') || previewUrl.startsWith('https://')) return previewUrl;
	return `${API_BASE}${previewUrl}`;
}
