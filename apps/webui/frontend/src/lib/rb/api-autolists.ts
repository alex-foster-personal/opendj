/**
 * Typed client for autolists routes (issue #2066).
 */
import { RbApiError } from './api-rb-error';
import type { SmartlistTrackRow } from './api-smartlists';
import type { AutolistSelection } from '$lib/smartlists/autolist-rule';
import { ApiError, api, unwrap } from '../api/client';

export interface AutolistGroup {
	id: string;
	title: string;
}

export interface AutolistBucket {
	id: string;
	label: string;
	count: number | null;
}

export interface AutolistIndexItem {
	stable_id: string;
	genre: string | null;
	rating: number | null;
	bpm: number | null;
}

export interface AutolistQueryResult {
	items: string[];
	tracks: SmartlistTrackRow[];
	total: number;
	offset: number;
	limit: number;
}

function _throwAutolistError(error: unknown): never {
	if (error instanceof ApiError) {
		throw new RbApiError(error.status, error.code, error.message);
	}
	throw error;
}

export async function listAutolistGroups(): Promise<AutolistGroup[]> {
	try {
		return (await unwrap(api.GET('/api/v1/autolists/groups', {}))) as AutolistGroup[];
	} catch (error) {
		_throwAutolistError(error);
	}
}

export async function listAutolistBuckets(
	group: 'genre' | 'rating' | 'bpm'
): Promise<AutolistBucket[]> {
	try {
		return (await unwrap(
			api.GET('/api/v1/autolists/buckets', {
				params: { query: { group } }
			})
		)) as AutolistBucket[];
	} catch (error) {
		_throwAutolistError(error);
	}
}

export async function fetchAutolistIndex(): Promise<AutolistIndexItem[]> {
	try {
		const data = (await unwrap(api.GET('/api/v1/autolists/index', {}))) as {
			items: AutolistIndexItem[];
		};
		return data.items;
	} catch (error) {
		_throwAutolistError(error);
	}
}

export async function queryAutolists(
	selection: AutolistSelection,
	offset = 0,
	limit = 500
): Promise<AutolistQueryResult> {
	try {
		return (await unwrap(
			api.POST('/api/v1/autolists/query', {
				body: { selection, offset, limit }
			})
		)) as unknown as AutolistQueryResult;
	} catch (error) {
		_throwAutolistError(error);
	}
}
