/** Fail-fast route-local client for the read-only play-analytics contract. */
import { API_BASE, ApiError, api, unwrap } from '$lib/api/client';
import {
	SHARE_STATES,
	type AnalyticsSession,
	type AnalyticsTrack,
	type PlayAnalyticsResponse,
	type ShareState
} from './types';

function asObject(value: unknown, context: string): Record<string, unknown> {
	if (typeof value !== 'object' || value === null || Array.isArray(value)) {
		throw new Error(`play-analytics: ${context} is not an object`);
	}
	return value as Record<string, unknown>;
}

function asString(value: unknown, context: string): string {
	if (typeof value !== 'string') throw new Error(`play-analytics: ${context} is not a string`);
	return value;
}

function asNullableString(value: unknown, context: string): string | null {
	if (value === null) return null;
	return asString(value, context);
}

function asNumber(value: unknown, context: string): number {
	if (typeof value !== 'number' || !Number.isFinite(value)) {
		throw new Error(`play-analytics: ${context} is not a finite number`);
	}
	return value;
}

function asArray(value: unknown, context: string): unknown[] {
	if (!Array.isArray(value)) throw new Error(`play-analytics: ${context} is not an array`);
	return value;
}

function asShareState(value: unknown, context: string): ShareState {
	const state = asString(value, context);
	if (!(SHARE_STATES as readonly string[]).includes(state)) {
		throw new Error(`play-analytics: ${context} has unknown value '${state}'`);
	}
	return state as ShareState;
}

function parseSession(value: unknown, index: number): AnalyticsSession {
	const context = `sessions[${index}]`;
	const row = asObject(value, context);
	return {
		session_id: asString(row.session_id, `${context}.session_id`),
		started_at: asString(row.started_at, `${context}.started_at`),
		ended_at: asNullableString(row.ended_at, `${context}.ended_at`),
		duration_s: row.duration_s === null ? null : asNumber(row.duration_s, `${context}.duration_s`),
		share_state: asShareState(row.share_state, `${context}.share_state`),
		play_count: asNumber(row.play_count, `${context}.play_count`),
		unique_track_count: asNumber(row.unique_track_count, `${context}.unique_track_count`)
	};
}

function parseTrack(value: unknown, index: number): AnalyticsTrack {
	const context = `top_tracks[${index}]`;
	const row = asObject(value, context);
	return {
		stable_id: asString(row.stable_id, `${context}.stable_id`),
		title: asNullableString(row.title, `${context}.title`),
		artist: asNullableString(row.artist, `${context}.artist`),
		play_count: asNumber(row.play_count, `${context}.play_count`),
		last_played_at: asString(row.last_played_at, `${context}.last_played_at`)
	};
}

export function validatePlayAnalyticsResponse(raw: unknown): PlayAnalyticsResponse {
	const response = asObject(raw, 'response');
	if (response.schema_version !== 1) {
		throw new Error(`play-analytics: unsupported schema_version '${String(response.schema_version)}'`);
	}
	const filters = asObject(response.filters, 'filters');
	const shareState = filters.share_state === null ? null : asShareState(filters.share_state, 'filters.share_state');
	const summary = asObject(response.summary, 'summary');
	return {
		schema_version: 1,
		filters: {
			share_state: shareState,
			limit: asNumber(filters.limit, 'filters.limit')
		},
		summary: {
			sessions: asNumber(summary.sessions, 'summary.sessions'),
			plays: asNumber(summary.plays, 'summary.plays'),
			unique_tracks: asNumber(summary.unique_tracks, 'summary.unique_tracks'),
			completed_duration_s: asNumber(
				summary.completed_duration_s,
				'summary.completed_duration_s'
			)
		},
		sessions: asArray(response.sessions, 'sessions').map(parseSession),
		top_tracks: asArray(response.top_tracks, 'top_tracks').map(parseTrack)
	};
}

/**
 * No sets have been recorded on this machine yet, so the event store file was
 * never created. That is the normal state of a new library, not a fault: the
 * page shows an empty state instead of the server's 503 detail and path.
 * Every other 503 (a missing table, a corrupt row) still fails loudly.
 */
export class NoEventStoreError extends Error {
	constructor() {
		super('play-analytics: no sets recorded yet (event store not created)');
		this.name = 'NoEventStoreError';
	}
}

/** The server's AnalyticsSchemaError text for a store file that does not exist. */
/** The prefix `apps/play_analytics/query.py` puts on a missing store's 503
 *  detail; `tests/play_analytics/test_surfaces.py` holds the server to it. */
export const NO_EVENT_STORE_DETAIL = 'event store does not exist:';

export function isNoEventStore(status: number, bodyText: string): boolean {
	if (status !== 503) return false;
	try {
		const detail = (JSON.parse(bodyText) as { detail?: unknown }).detail;
		return typeof detail === 'string' && detail.startsWith(NO_EVENT_STORE_DETAIL);
	} catch {
		return false;
	}
}

export async function fetchPlayAnalytics(
	shareState: ShareState | null,
	limit: number
): Promise<PlayAnalyticsResponse> {
	const query = new URLSearchParams();
	if (shareState !== null) query.set('share_state', shareState);
	query.set('limit', String(limit));
	const endpoint = `${API_BASE}/api/play-analytics?${query.toString()}`;
	let data: unknown;
	try {
		data = await unwrap(
			api.GET('/api/play-analytics', {
				params: { query: { share_state: shareState, limit } }
			})
		);
	} catch (error) {
		if (error instanceof ApiError) {
			const bodyText = error.bodyText;
			if (isNoEventStore(error.status, bodyText)) throw new NoEventStoreError();
			throw new Error(
				`GET /api/play-analytics failed: ${error.status} ${bodyText.slice(0, 300)}`
			);
		}
		throw new Error(
			`daemon unreachable at ${endpoint} (${error instanceof Error ? error.message : String(error)})`
		);
	}
	return validatePlayAnalyticsResponse(data);
}
