/** Thin client for settings AI search / apply endpoints. */

import type { components } from '../api-types';
import { ApiError, api, unwrap } from '../api/client';

// Generated schemas match field-for-field for search/proposal; AiApplyOut
// keeps the hand-written required nullables (schema marks error/proposal optional).
export type AiSearchOut = components['schemas']['AiSearchOut'];
export type AiApplyProposal = components['schemas']['AiApplyProposal'];

export interface AiApplyOut {
	ok: boolean;
	proposal: AiApplyProposal | null;
	error: string | null;
	model: string;
}

function _mapError(path: string, error: unknown): never {
	if (error instanceof ApiError) {
		const detail =
			error.body && typeof error.body === 'object' && error.body !== null && 'detail' in error.body
				? JSON.stringify((error.body as { detail: unknown }).detail)
				: error.message;
		throw new Error(`${path} failed (${error.status}): ${detail}`);
	}
	throw error;
}

export async function aiSearchSettings(query: string, catalogIds: string[]): Promise<AiSearchOut> {
	try {
		return await unwrap(
			api.POST('/api/v1/settings/ai-search', {
				body: { query, catalog_ids: catalogIds }
			})
		);
	} catch (error) {
		_mapError('/api/v1/settings/ai-search', error);
	}
}

export async function aiApplySetting(instruction: string): Promise<AiApplyOut> {
	try {
		const data = await unwrap(
			api.POST('/api/v1/settings/ai-apply', {
				body: { instruction }
			})
		);
		// Schema marks error/proposal optional; callers expect explicit nulls.
		return {
			ok: data.ok,
			model: data.model,
			proposal: data.proposal ?? null,
			error: data.error ?? null
		};
	} catch (error) {
		_mapError('/api/v1/settings/ai-apply', error);
	}
}
