/** Fail-fast route-local client for the LIBUX-06 library-wheel contract. */
import { API_BASE, ApiError, api, unwrap } from '$lib/api/client';
import { AXIS_KEYS, type AxisKey, type LibraryWheelResponse, type WheelAxis, type WheelFamily, type WheelGenre, type WheelTrack } from './types';

function asObject(value: unknown, context: string): Record<string, unknown> {
	if (typeof value !== 'object' || value === null || Array.isArray(value)) {
		throw new Error(`library-wheel: ${context} is not an object`);
	}
	return value as Record<string, unknown>;
}

function asString(value: unknown, context: string): string {
	if (typeof value !== 'string') throw new Error(`library-wheel: ${context} is not a string`);
	return value;
}

function asNullableString(value: unknown, context: string): string | null {
	if (value === null) return null;
	return asString(value, context);
}

function asNullableNumber(value: unknown, context: string): number | null {
	if (value === null) return null;
	if (typeof value !== 'number' || !Number.isFinite(value)) {
		throw new Error(`library-wheel: ${context} is not a finite number or null`);
	}
	return value;
}

function asNumber(value: unknown, context: string): number {
	if (typeof value !== 'number' || !Number.isFinite(value)) {
		throw new Error(`library-wheel: ${context} is not a finite number`);
	}
	return value;
}

function asBoolean(value: unknown, context: string): boolean {
	if (typeof value !== 'boolean') throw new Error(`library-wheel: ${context} is not a boolean`);
	return value;
}

function asArray(value: unknown, context: string): unknown[] {
	if (!Array.isArray(value)) throw new Error(`library-wheel: ${context} is not an array`);
	return value;
}

function asAxisKey(value: unknown, context: string): AxisKey {
	const key = asString(value, context);
	if (!(AXIS_KEYS as readonly string[]).includes(key)) {
		throw new Error(`library-wheel: ${context} has unknown value '${key}'`);
	}
	return key as AxisKey;
}

function parseAxis(value: unknown, index: number): WheelAxis {
	const context = `axes[${index}]`;
	const row = asObject(value, context);
	return {
		key: asAxisKey(row.key, `${context}.key`),
		label: asString(row.label, `${context}.label`),
		enabled: asBoolean(row.enabled, `${context}.enabled`),
		reason: asNullableString(row.reason, `${context}.reason`)
	};
}

function parseTrack(value: unknown, context: string): WheelTrack {
	const row = asObject(value, context);
	return {
		stable_id: asString(row.stable_id, `${context}.stable_id`),
		title: asNullableString(row.title, `${context}.title`),
		artist: asNullableString(row.artist, `${context}.artist`),
		genre: asNullableString(row.genre, `${context}.genre`),
		axis_value: asNullableNumber(row.axis_value, `${context}.axis_value`),
		axis_title: asNullableString(row.axis_title, `${context}.axis_title`)
	};
}

function parseGenre(value: unknown, context: string): WheelGenre {
	const row = asObject(value, context);
	const tracks = asArray(row.tracks, `${context}.tracks`);
	return {
		tag: asString(row.tag, `${context}.tag`),
		track_count: asNumber(row.track_count, `${context}.track_count`),
		tracks: tracks.map((t, i) => parseTrack(t, `${context}.tracks[${i}]`))
	};
}

function parseFamily(value: unknown, index: number): WheelFamily {
	const context = `families[${index}]`;
	const row = asObject(value, context);
	const genres = asArray(row.genres, `${context}.genres`);
	return {
		name: asString(row.name, `${context}.name`),
		color: asString(row.color, `${context}.color`),
		track_count: asNumber(row.track_count, `${context}.track_count`),
		genres: genres.map((g, i) => parseGenre(g, `${context}.genres[${i}]`))
	};
}

export function validateLibraryWheelResponse(raw: unknown): LibraryWheelResponse {
	const response = asObject(raw, 'response');
	if (response.schema_version !== 1) {
		throw new Error(
			`library-wheel: unsupported schema_version '${String(response.schema_version)}'`
		);
	}
	return {
		schema_version: 1,
		axis: asAxisKey(response.axis, 'axis'),
		axes: asArray(response.axes, 'axes').map(parseAxis),
		selected_axis_enabled: asBoolean(response.selected_axis_enabled, 'selected_axis_enabled'),
		selected_axis_reason: asNullableString(
			response.selected_axis_reason,
			'selected_axis_reason'
		),
		total_tracks: asNumber(response.total_tracks, 'total_tracks'),
		unclassified_track_count: asNumber(
			response.unclassified_track_count,
			'unclassified_track_count'
		),
		families: asArray(response.families, 'families').map(parseFamily)
	};
}

export async function fetchLibraryWheel(axis: AxisKey): Promise<LibraryWheelResponse> {
	const endpoint = `${API_BASE}/api/v1/library/wheel?axis=${axis}`;
	let data: unknown;
	try {
		data = await unwrap(api.GET('/api/v1/library/wheel', { params: { query: { axis } } }));
	} catch (error) {
		if (error instanceof ApiError) {
			const bodyText = await error.response.text();
			throw new Error(
				`GET /api/v1/library/wheel failed: ${error.status} ${bodyText.slice(0, 300)}`
			);
		}
		throw new Error(
			`daemon unreachable at ${endpoint} (${error instanceof Error ? error.message : String(error)})`
		);
	}
	return validateLibraryWheelResponse(data);
}
