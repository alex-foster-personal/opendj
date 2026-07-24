/** Thin client for settings AI search / apply endpoints. */

export interface AiSearchOut {
	ids: string[];
	model: string;
}

export interface AiApplyProposal {
	key: string;
	value: boolean | string;
	rationale: string;
}

export interface AiApplyOut {
	ok: boolean;
	proposal: AiApplyProposal | null;
	error: string | null;
	model: string;
}

async function _post<T>(path: string, body: unknown): Promise<T> {
	const r = await fetch(path, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify(body)
	});
	const text = await r.text();
	let parsed: unknown = null;
	try {
		parsed = text ? JSON.parse(text) : null;
	} catch {
		throw new Error(`${path} returned non-JSON (${r.status}): ${text.slice(0, 200)}`);
	}
	if (!r.ok) {
		const detail =
			parsed && typeof parsed === 'object' && parsed !== null && 'detail' in parsed
				? JSON.stringify((parsed as { detail: unknown }).detail)
				: text.slice(0, 300);
		throw new Error(`${path} failed (${r.status}): ${detail}`);
	}
	return parsed as T;
}

export function aiSearchSettings(query: string, catalogIds: string[]): Promise<AiSearchOut> {
	return _post<AiSearchOut>('/api/v1/settings/ai-search', { query, catalog_ids: catalogIds });
}

export function aiApplySetting(instruction: string): Promise<AiApplyOut> {
	return _post<AiApplyOut>('/api/v1/settings/ai-apply', { instruction });
}
