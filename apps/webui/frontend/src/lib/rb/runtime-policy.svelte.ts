/**
 * Server-owned runtime policy thresholds (GET /api/v1/settings).
 * Boolean prefs such as hide_broken_links stay in ui-prefs; only numeric
 * policy lives here (issue #284, POLICY-01).
 */

import { getSettings, type SettingItem } from '$lib/api';
import { defaultAnlzPoints, setAnlzPointsDefault } from '$lib/rb/runtime-policy-points';
import {
	playlistMostlyBroken as playlistMostlyBrokenByCount,
	type PlaylistAvailability
} from '$lib/rb/playlist-broken-filter';

/** Shipped server defaults; used until hydration succeeds. */
const SHIPPED_HIDE_BROKEN_MIN_TRACKS = 4;
const SHIPPED_ANLZ_POINTS_DEFAULT = 38400;
const SHIPPED_ANLZ_POINTS_MIN = 100;
const SHIPPED_ANLZ_POINTS_MAX = 38400;
const SHIPPED_FILE_EXISTS_TTL_S = 30.0;

export type { PlaylistAvailability };

export const runtimePolicy = $state({
	hide_broken_playlist_min_available_tracks: SHIPPED_HIDE_BROKEN_MIN_TRACKS,
	anlz_points_default: SHIPPED_ANLZ_POINTS_DEFAULT,
	anlz_points_min: SHIPPED_ANLZ_POINTS_MIN,
	anlz_points_max: SHIPPED_ANLZ_POINTS_MAX,
	file_exists_ttl_s: SHIPPED_FILE_EXISTS_TTL_S,
	config_ready: false
});

function _settingNumber(items: SettingItem[], key: string): number {
	const item = items.find((i) => i.key === key);
	if (item === undefined) {
		throw new Error(`runtime policy: ${key} missing from /api/v1/settings`);
	}
	const n = typeof item.value === 'number' ? item.value : Number(item.value);
	if (!Number.isFinite(n)) {
		throw new Error(
			`runtime policy: ${key} is not a finite number (${String(item.value)})`
		);
	}
	return n;
}

/** Pull policy knobs from GET /api/v1/settings; fail fast on missing keys. */
export async function hydrateRuntimePolicy(): Promise<void> {
	const settings = await getSettings();
	const items = settings.groups.flatMap((g) => g.items);
	runtimePolicy.hide_broken_playlist_min_available_tracks = _settingNumber(
		items,
		'hide_broken_playlist_min_available_tracks'
	);
	runtimePolicy.anlz_points_default = _settingNumber(items, 'anlz_points_default');
	runtimePolicy.anlz_points_min = _settingNumber(items, 'anlz_points_min');
	runtimePolicy.anlz_points_max = _settingNumber(items, 'anlz_points_max');
	runtimePolicy.file_exists_ttl_s = _settingNumber(items, 'file_exists_ttl_s');
	setAnlzPointsDefault(runtimePolicy.anlz_points_default);
	runtimePolicy.config_ready = true;
}

export { defaultAnlzPoints };

export function playlistMostlyBroken(p: PlaylistAvailability): boolean {
	return playlistMostlyBrokenByCount(
		p.available_count,
		runtimePolicy.hide_broken_playlist_min_available_tracks
	);
}

function _hideBrokenMinTracks(): number {
	return runtimePolicy.hide_broken_playlist_min_available_tracks;
}

export function formatMostlyBrokenTooltip(): string {
	const n = _hideBrokenMinTracks();
	return `Fewer than ${n} tracks in this playlist are playable`;
}

export function formatHideBrokenCheckboxTooltip(): string {
	const n = _hideBrokenMinTracks();
	return (
		`Show tracks whose audio file is missing on disk. Unchecking also hides playlists ` +
		`with fewer than ${n} playable tracks, including empty ones.`
	);
}
