import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const POLICY_SOURCE = new URL('../../src/lib/rb/runtime-policy.svelte.ts', import.meta.url);

function readPolicySource() {
	return readFileSync(POLICY_SOURCE, 'utf8');
}

test('runtime policy ships the same defaults as the backend module', () => {
	const source = readPolicySource();
	assert.match(source, /SHIPPED_HIDE_BROKEN_RATIO = 0\.3/);
	assert.match(source, /SHIPPED_ANLZ_POINTS_DEFAULT = 38400/);
	assert.match(source, /SHIPPED_ANLZ_POINTS_MIN = 100/);
	assert.match(source, /SHIPPED_ANLZ_POINTS_MAX = 38400/);
	assert.match(source, /SHIPPED_FILE_EXISTS_TTL_S = 30\.0/);
});

test('playlistMostlyBroken keeps fast-list, empty-playlist, and ratio rules', () => {
	const source = readPolicySource();
	assert.match(source, /if \(p\.available_count < 0\) return false;/);
	assert.match(source, /if \(p\.track_count === 0\) return p\.available_count === 0;/);
	assert.match(
		source,
		/p\.available_count \/ p\.track_count <\s*runtimePolicy\.hide_broken_playlist_min_available_ratio/
	);
});

test('formatMostlyBrokenTooltip derives percent from the hydrated ratio', () => {
	const source = readPolicySource();
	assert.match(
		source,
		/Math\.round\(runtimePolicy\.hide_broken_playlist_min_available_ratio \* 100\)/
	);
	assert.match(source, /Fewer than \$\{_mostlyBrokenPercent\(\)\}% of tracks in this playlist are playable/);
});

test('hydrateRuntimePolicy reads all five policy keys from settings', () => {
	const source = readPolicySource();
	for (const key of [
		'hide_broken_playlist_min_available_ratio',
		'anlz_points_default',
		'anlz_points_min',
		'anlz_points_max',
		'file_exists_ttl_s'
	]) {
		assert.match(source, new RegExp(`'${key}'`));
	}
	assert.match(source, /runtimePolicy\.config_ready = true/);
});
