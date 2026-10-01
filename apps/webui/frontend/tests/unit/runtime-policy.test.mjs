import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const POLICY_SOURCE = new URL('../../src/lib/rb/runtime-policy.svelte.ts', import.meta.url);
const FILTER_SOURCE = new URL('../../src/lib/rb/playlist-broken-filter.ts', import.meta.url);

function readPolicySource() {
	return readFileSync(POLICY_SOURCE, 'utf8');
}

test('runtime policy ships the same defaults as the backend module', () => {
	const source = readPolicySource();
	assert.match(source, /SHIPPED_HIDE_BROKEN_MIN_TRACKS = 4/);
	assert.match(source, /SHIPPED_ANLZ_POINTS_DEFAULT = 38400/);
	assert.match(source, /SHIPPED_ANLZ_POINTS_MIN = 100/);
	assert.match(source, /SHIPPED_ANLZ_POINTS_MAX = 38400/);
	assert.match(source, /SHIPPED_FILE_EXISTS_TTL_S = 30\.0/);
});

test('playlistMostlyBroken delegates to playlist-broken-filter with hydrated min tracks', () => {
	const source = readPolicySource();
	assert.match(source, /from '\$lib\/rb\/playlist-broken-filter'/);
	assert.match(
		source,
		/playlistMostlyBrokenByCount\(\s*p\.available_count,\s*runtimePolicy\.hide_broken_playlist_min_available_tracks\s*\)/
	);
	const filter = readFileSync(FILTER_SOURCE, 'utf8');
	assert.match(filter, /return availableCount < minVisibleTracks;/);
});

test('formatMostlyBrokenTooltip uses the hydrated min playable-track count', () => {
	const source = readPolicySource();
	assert.match(source, /Fewer than \$\{n\} tracks in this playlist are playable/);
	assert.match(source, /runtimePolicy\.hide_broken_playlist_min_available_tracks/);
});

test('hydrateRuntimePolicy reads all five policy keys from settings', () => {
	const source = readPolicySource();
	for (const key of [
		'hide_broken_playlist_min_available_tracks',
		'anlz_points_default',
		'anlz_points_min',
		'anlz_points_max',
		'file_exists_ttl_s'
	]) {
		assert.match(source, new RegExp(`'${key}'`));
	}
	assert.match(source, /runtimePolicy\.config_ready = true/);
});
