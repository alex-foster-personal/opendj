export const SHARE_STATES = ['private', 'shared_local', 'shared_cloud'] as const;
export type ShareState = (typeof SHARE_STATES)[number];

export interface AnalyticsFilters {
	share_state: ShareState | null;
	limit: number;
}

export interface AnalyticsSummary {
	sessions: number;
	plays: number;
	unique_tracks: number;
	completed_duration_s: number;
}

export interface AnalyticsSession {
	session_id: string;
	started_at: string;
	ended_at: string | null;
	duration_s: number | null;
	share_state: ShareState;
	play_count: number;
	unique_track_count: number;
}

export interface AnalyticsTrack {
	stable_id: string;
	title: string | null;
	artist: string | null;
	play_count: number;
	last_played_at: string;
}

export interface PlayAnalyticsResponse {
	schema_version: 1;
	filters: AnalyticsFilters;
	summary: AnalyticsSummary;
	sessions: AnalyticsSession[];
	top_tracks: AnalyticsTrack[];
}
