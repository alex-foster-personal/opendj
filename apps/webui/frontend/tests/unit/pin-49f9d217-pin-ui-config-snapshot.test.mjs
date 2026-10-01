/**
 * Pin 49f9d217: a comment pin carries a compact snapshot of non-secret UI
 * config, so an agent debugging it knows the route, mode and engine in play.
 *
 * [if] the snapshot carries a query string, a home path or free text [then] broken
 * [if] a switch is not a boolean [then] broken
 * [if] the pin POST body omits the snapshot [then] broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

const { buildPinUiConfig, PIN_UI_CONFIG_SWITCHES } = await loadTypeScriptModule(
	'src/lib/rb/feedback-pin-ui-config.ts'
);

// The daemon's own patterns (apps/webui/server/routes/feedback.py PinUiConfig).
const SLUG = /^[a-z0-9][a-z0-9_-]{0,31}$/;
const SWITCH_KEY = /^[a-z][a-z0-9_.]{0,47}$/;
const ROUTE = /^\/[A-Za-z0-9/_.-]{0,119}$/;
const MAX_SWITCHES = 24;

const PREFS = {
	app_mode: 'performance',
	perf_tier: 'auto',
	show_stems: true,
	beat_sync_max: false,
	auto_play_enabled: true,
	next_only_filter: false,
	remixes_filter: false,
	vocals_filter: true,
	available_offline_filter: false,
	hide_broken_links: false,
	jog_radial_waveform: false,
	show_agent_pins: true,
	// Things a snapshot must never pick up.
	last_playlist: { id: 7, name: 'A Private Playlist Name' },
	auto_sync: { rekordbox: true },
	playlist_tree_width: 300
};

test('snapshot names route, app mode, engine mode, perf tier and the switches', () => {
	const cfg = buildPinUiConfig({ pathname: '/performance', prefs: PREFS, rustEngine: false });
	assert.deepEqual(Object.keys(cfg).sort(), ['app_mode', 'engine_mode', 'perf_tier', 'route', 'switches']);
	assert.equal(cfg.route, '/performance');
	assert.equal(cfg.app_mode, 'performance');
	assert.equal(cfg.engine_mode, 'webaudio');
	assert.equal(cfg.perf_tier, 'auto');
	assert.equal(cfg.switches.show_stems, true);
	assert.equal(cfg.switches.vocals_filter, true);
	assert.equal(cfg.switches.beat_sync_max, false);
	assert.equal(buildPinUiConfig({ pathname: '/', prefs: PREFS, rustEngine: true }).engine_mode, 'rust');
});

test('every field fits the daemon contract: slugs, booleans, bounded switches', () => {
	const cfg = buildPinUiConfig({ pathname: '/prep', prefs: PREFS, rustEngine: true });
	assert.match(cfg.route, ROUTE);
	for (const k of ['app_mode', 'engine_mode', 'perf_tier']) assert.match(cfg[k], SLUG, k);
	const keys = Object.keys(cfg.switches);
	assert.deepEqual(keys, [...PIN_UI_CONFIG_SWITCHES]);
	assert.ok(keys.length > 0 && keys.length <= MAX_SWITCHES);
	for (const k of keys) {
		assert.match(k, SWITCH_KEY, k);
		assert.equal(typeof cfg.switches[k], 'boolean', k);
	}
});

test('query string and fragment never reach the route', () => {
	const cfg = buildPinUiConfig({
		pathname: '/performance?engine=rust&token=abc#frag',
		prefs: PREFS,
		rustEngine: false
	});
	assert.equal(cfg.route, '/performance');
});

test('nothing outside the declared switches is copied from prefs', () => {
	const json = JSON.stringify(buildPinUiConfig({ pathname: '/', prefs: PREFS, rustEngine: false }));
	assert.ok(!json.includes('Private Playlist'), 'a playlist name leaked into the snapshot');
	assert.ok(!json.includes('auto_sync'));
	assert.ok(!json.includes('playlist_tree_width'));
});

test('a pref that is not a boolean is refused, never coerced', () => {
	assert.throws(
		() =>
			buildPinUiConfig({
				pathname: '/',
				prefs: { ...PREFS, show_stems: '/Users/someone/Music' },
				rustEngine: false
			}),
		/show_stems/
	);
});

test('a route that is not an app route is refused', () => {
	assert.throws(
		() => buildPinUiConfig({ pathname: 'performance', prefs: PREFS, rustEngine: false }),
		/route/
	);
});

test('the pin POST body sends the snapshot', () => {
	const src = readFileSync(
		new URL('../../src/lib/components/rb/FeedbackPinDraftBubble.svelte', import.meta.url),
		'utf8'
	);
	const body = src.slice(src.indexOf('const pinBody = {'), src.indexOf('} as const;', src.indexOf('const pinBody = {')));
	assert.ok(body.length > 0, 'pinBody literal not found');
	assert.match(body, /ui_config:\s*buildPinUiConfig\(\{/);
	assert.match(body, /pathname:\s*submitted\.page/);
	assert.match(body, /rustEngine:\s*rustMode\.enabled/);
});

test('the pin card shows the stamped user and the config snapshot', () => {
	const src = readFileSync(
		new URL('../../src/lib/components/rb/FeedbackPinCard.svelte', import.meta.url),
		'utf8'
	);
	assert.match(src, /\{#if pin\.environment\.user_email\}/);
	assert.match(src, /\{cfg\.app_mode\} \/ \{cfg\.engine_mode\} \/ \{cfg\.perf_tier\}/);
});
