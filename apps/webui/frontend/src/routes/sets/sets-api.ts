/** Typed HTTP contract for the Session/REC surface. */

const DEFAULT_BASE = typeof window === 'undefined' ? 'http://127.0.0.1:8585' : '';
const ENV_BASE = import.meta.env.VITE_API_BASE as string | undefined;
export const SETS_API_BASE = ENV_BASE ?? DEFAULT_BASE;
const SETS_PATH = '/api/sets';

export interface RecorderStatus {
	active: boolean;
	session_id: string | null;
	pid: number | null;
	owned: boolean;
	recoverable: boolean;
}

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

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
	const response = await fetch(`${SETS_API_BASE}${SETS_PATH}${path}`, {
		...init,
		headers: {
			Accept: 'application/json',
			...(init?.body ? { 'Content-Type': 'application/json' } : {}),
			...init?.headers
		}
	});
	if (!response.ok) {
		let detail = `${response.status} ${response.statusText}`;
		try {
			const payload = (await response.json()) as { detail?: string };
			if (payload.detail) detail = payload.detail;
		} catch {
			// The HTTP status remains the explicit terminal error.
		}
		throw new Error(detail);
	}
	return (await response.json()) as T;
}

export function getRecorderStatus(): Promise<RecorderStatus> {
	return requestJson<RecorderStatus>('/recorder');
}

export function startRecorder(input: RecorderStartInput): Promise<RecorderStatus> {
	return requestJson<RecorderStatus>('/recorder/start', {
		method: 'POST',
		body: JSON.stringify(input)
	});
}

export function stopRecorder(sessionId: string): Promise<RecorderStatus> {
	return requestJson<RecorderStatus>(`/recorder/${encodeURIComponent(sessionId)}/stop`, {
		method: 'POST'
	});
}

export function recoverRecorder(sessionId: string, expectedPid: number): Promise<RecorderStatus> {
	return requestJson<RecorderStatus>(`/recorder/${encodeURIComponent(sessionId)}/recover`, {
		method: 'POST',
		body: JSON.stringify({ expected_pid: expectedPid })
	});
}

export function listSessions(): Promise<SessionSummary[]> {
	return requestJson<SessionSummary[]>('');
}

export function getSession(sessionId: string): Promise<SessionDetail> {
	return requestJson<SessionDetail>(`/${encodeURIComponent(sessionId)}`);
}

export function getTimeline(sessionId: string): Promise<TimelineEvent[]> {
	return requestTimeline(`/${encodeURIComponent(sessionId)}/timeline`);
}

export function getTransitions(sessionId: string): Promise<Transition[]> {
	return requestJson<Transition[]>(`/${encodeURIComponent(sessionId)}/transitions`);
}

export function sessionAudioUrl(sessionId: string, segmentName: string): string {
	return `${SETS_API_BASE}${SETS_PATH}/${encodeURIComponent(sessionId)}/audio/${encodeURIComponent(segmentName)}`;
}

async function requestTimeline(path: string): Promise<TimelineEvent[]> {
	const response = await fetch(`${SETS_API_BASE}${SETS_PATH}${path}`, {
		headers: { Accept: 'application/x-ndjson' }
	});
	if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
	return (await response.text())
		.split('\n')
		.filter((line) => line.trim() !== '')
		.map((line, index) => {
			try {
				return JSON.parse(line) as TimelineEvent;
			} catch (error) {
				throw new Error(`Malformed timeline event at line ${index + 1}: ${error}`);
			}
		});
}
