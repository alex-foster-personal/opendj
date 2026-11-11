/**
 * Genre inference HTTP client (GENRE-03, issue #11310 on-load enqueue).
 */
import { API_BASE, RbApiError } from '$lib/api';

export type GenreEnqueueResult = {
	lane: 'genre';
	queued: number;
	requested: number;
	eligible: number;
};

async function _parse<T>(res: Response, fallbackCode: string): Promise<T> {
	if (!res.ok) {
		let code = fallbackCode;
		let message = res.statusText;
		let body: unknown = null;
		try {
			body = await res.json();
			const detail = (body as { detail?: { code?: string; message?: string } }).detail;
			if (detail?.code) code = detail.code;
			if (detail?.message) message = detail.message;
		} catch {
			/* keep statusText */
		}
		throw new RbApiError(res.status, code, message, body);
	}
	return (await res.json()) as T;
}

/** Queue genre inference for tracks looked at on /performance (server filters ids). */
export async function enqueueGenreInference(stable_ids: string[]): Promise<GenreEnqueueResult> {
	if (stable_ids.length === 0) {
		return { lane: 'genre', queued: 0, requested: 0, eligible: 0 };
	}
	const res = await fetch(`${API_BASE}/api/v1/genres/enqueue`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ stable_ids })
	});
	return _parse<GenreEnqueueResult>(res, 'genre_enqueue_failed');
}
