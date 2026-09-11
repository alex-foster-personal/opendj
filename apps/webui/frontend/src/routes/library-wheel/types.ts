export const AXIS_KEYS = [
	'genre',
	'decade',
	'play_count',
	'popularity',
	'overplayed_ness',
	'playlist',
	'set_played_in'
] as const;
export type AxisKey = (typeof AXIS_KEYS)[number];

export interface WheelAxis {
	key: AxisKey;
	label: string;
	enabled: boolean;
	reason: string | null;
}

export interface WheelTrack {
	stable_id: string;
	title: string | null;
	artist: string | null;
	genre: string | null;
	axis_value: number | null;
	axis_title: string | null;
}

export interface WheelGenre {
	tag: string;
	track_count: number;
	tracks: WheelTrack[];
}

export interface WheelFamily {
	name: string;
	color: string;
	track_count: number;
	genres: WheelGenre[];
}

export interface LibraryWheelResponse {
	schema_version: 1;
	axis: AxisKey;
	axes: WheelAxis[];
	selected_axis_enabled: boolean;
	selected_axis_reason: string | null;
	total_tracks: number;
	unclassified_track_count: number;
	families: WheelFamily[];
}
