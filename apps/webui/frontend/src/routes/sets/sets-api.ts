/** Typed HTTP contract for the Session/REC surface. */

import type { components } from '$lib/api-types';
import { API_BASE, ApiError, api, unwrap } from '$lib/api/client';

export const SETS_API_BASE = API_BASE;
const SETS_PATH = '/api/sets';

export type RecorderStatus = components['schemas']['RecorderStatus'];

export interface RecorderStartInput {
	session_id: string | null;
	ffmpeg_device_idx: number;
	sources: Array<'djay_monitor' | 'rb_history'>;
}

export interface SessionSummary {
	session_id: string;
	started_at: string;
	ended_at: string | null;
	duration_s: number | null;
	event_count: number;
	transition_count: number;
	share_state: 'private' | 'shared_local' | 'shared_cloud';
}

export interface AudioSegment {
	name: string;
	start_t_s: number;
	duration_s: number | null;
	size_bytes: number;
}

export interface Transition {
	idx: number;
	t_change_s: number;
	from_deck: string | null;
	to_deck: string | null;
	from_track: string | null;
	to_track: string | null;
	predicted_class: string;
	confidence: number;
}

export interface SessionDetail {
	summary: SessionSummary;
	manifest: {
		capture_device: string;
		deck_sources: string[];
		watermark: string;
	};
	segments: AudioSegment[];
}

export interface TimelineEvent {
	session_id: string;
	timestamp_s: number;
	wall_clock: string;
	action: string;
	source: string;
	deck: string | null;
	track_stable_id: string | null;
	value: Record<string, unknown>;
}

/** Preserve the pre-client Error(detail) contract for non-2xx responses. */
function rethrowSetsError(error: unknown): never {
	if (error instanceof ApiError) {
		const d = (error.body as { detail?: unknown } | null)?.detail;
		throw new Error(typeof d === 'string' ? d : `${error.status} ${error.response.statusText}`);
	}
	throw error;
}

export async function getRecorderStatus(): Promise<RecorderStatus> {
	try {
		return await unwrap(api.GET('/api/sets/recorder', {}));
	} catch (error) {
		rethrowSetsError(error);
	}
}

export async function startRecorder(input: RecorderStartInput): Promise<RecorderStatus> {
	try {
		return await unwrap(
			api.POST('/api/sets/recorder/start', {
				body: input
			})
		);
	} catch (error) {
		rethrowSetsError(error);
	}
}

export async function stopRecorder(sessionId: string): Promise<RecorderStatus> {
	try {
		return await unwrap(
			api.POST('/api/sets/recorder/{session_id}/stop', {
				params: { path: { session_id: sessionId } }
			})
		);
	} catch (error) {
		rethrowSetsError(error);
	}
}

export async function recoverRecorder(
	sessionId: string,
	expectedPid: number
): Promise<RecorderStatus> {
	try {
		return await unwrap(
			api.POST('/api/sets/recorder/{session_id}/recover', {
				params: { path: { session_id: sessionId } },
				body: { expected_pid: expectedPid }
			})
		);
	} catch (error) {
		rethrowSetsError(error);
	}
}

export async function listSessions(): Promise<SessionSummary[]> {
	try {
		return (await unwrap(api.GET('/api/sets', {}))) as SessionSummary[];
	} catch (error) {
		rethrowSetsError(error);
	}
}

export async function getSession(sessionId: string): Promise<SessionDetail> {
	try {
		return (await unwrap(
			api.GET('/api/sets/{session_id}', {
				params: { path: { session_id: sessionId } }
			})
		)) as SessionDetail;
	} catch (error) {
		rethrowSetsError(error);
	}
}

export async function getTimeline(sessionId: string): Promise<TimelineEvent[]> {
	try {
		const { data } = await api.GET('/api/sets/{session_id}/timeline', {
			params: { path: { session_id: sessionId } },
			parseAs: 'text'
		});
		return String(data ?? '')
			.split('\n')
			.filter((line) => line.trim() !== '')
			.map((line, index) => {
				try {
					return JSON.parse(line) as TimelineEvent;
				} catch (error) {
					throw new Error(`Malformed timeline event at line ${index + 1}: ${error}`);
				}
			});
	} catch (error) {
		rethrowSetsError(error);
	}
}

export async function getTransitions(sessionId: string): Promise<Transition[]> {
	try {
		return (await unwrap(
			api.GET('/api/sets/{session_id}/transitions', {
				params: { path: { session_id: sessionId } }
			})
		)) as Transition[];
	} catch (error) {
		rethrowSetsError(error);
	}
}

export function sessionAudioUrl(sessionId: string, segmentName: string): string {
	return `${SETS_API_BASE}${SETS_PATH}/${encodeURIComponent(sessionId)}/audio/${encodeURIComponent(segmentName)}`;
}
