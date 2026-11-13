/**
 * PLAYS-9745: play History in the browser (rekordbox History, mimicked).
 *
 * The fetch client runs against a recorded-shape fake fetch (no server here);
 * the view renders through Svelte SSR, so clicks and the lazy panel load are
 * not exercised (e2e not run).
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://history-view.example.test';

const DAYS = {
	rollover_hour: 6,
	days: [
		{ date: '2026-11-13', count: 3 },
		{ date: '2026-11-06', count: 1 }
	]
};
const NIGHT = {
	date: '2026-11-13',
	start: '2026-11-13T06:00:00.000Z',
	end: '2026-11-14T06:00:00.000Z',
	plays: [
		{
			position: 1,
			stable_id: 'a',
			play_id: 'p1',
			played_at: '2026-11-13T22:05:00.000Z',
			deck: 1,
			source: 'opendj'
		},
		{
			position: 2,
			stable_id: 'gone',
			play_id: 'p2',
			played_at: '2026-11-13T23:59:00.000Z',
			deck: 2,
			source: 'opendj'
		},
		{
			position: 3,
			stable_id: 'a',
			play_id: null,
			played_at: '2026-11-14T00:40:00.000Z',
			deck: null,
			source: 'rekordbox'
		}
	]
};
const TRACKS = [{ stable_id: 'a', title: 'Strings of Life', artist: 'Rhythim Is Rhythim' }];

let api;
let view;
let ssr;

before(async () => {
	api = await loadTypeScriptModule('src/lib/history/history-api.ts', { viteApiBase: API_BASE });
	view = await loadTypeScriptModule('src/lib/history/history-view.ts');
	ssr = await loadSvelteSsrModule(
		[
			"export { default as HistoryPanelView } from '$lib/components/rb/browser/HistoryPanelView.svelte';",
			"export { default as IconRail } from '$lib/components/rb/browser/IconRail.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

function fakeFetch(calls, status, body) {
	return async (url, init = {}) => {
		calls.push({ url: String(url), method: init.method ?? 'GET', body: init.body });
		return new Response(JSON.stringify(body), {
			status,
			headers: { 'Content-Type': 'application/json' }
		});
	};
}

const noop = () => {};

function renderPanel(props) {
	return ssr.render(ssr.HistoryPanelView, {
		props: {
			days: [],
			loading: false,
			error: null,
			night: null,
			plays: [],
			playsLoading: false,
			playsError: null,
			saving: false,
			oncollection: noop,
			onopen: noop,
			onback: noop,
			onsave: noop,
			ondaymenu: noop,
			onplaymenu: noop,
			...props
		}
	}).body;
}

// REQ: PLAYS-14424
test('the stopwatch rail entry is live and lit while History is open', () => {
	const html = ssr.render(ssr.IconRail, {
		props: { source: 'collection', onspotify: noop, oncollection: noop, historyOpen: true }
	}).body;
	const tag = /<button\b[^>]*data-testid="rail-history"[^>]*>/.exec(html)?.[0];
	assert.ok(tag, 'if the rail has no History entry then broken');
	assert.doesNotMatch(tag, /\sdisabled/, 'if the History glyph is still inert then broken');
	assert.match(tag, /title="History - /);
	assert.match(
		tag,
		/aria-pressed="true"/,
		'if the open History does not light the glyph then broken'
	);
});

test('the client reads the nights list from GET /api/v1/history', async () => {
	const calls = [];
	const out = await api.fetchHistoryDays(fakeFetch(calls, 200, DAYS));
	assert.equal(calls[0].url, `${API_BASE}/api/v1/history`);
	assert.deepEqual(
		out.days.map((day) => day.date),
		['2026-11-13', '2026-11-06']
	);
});

test('the client reads one night from GET /api/v1/history/{day}, tz passed through', async () => {
	const calls = [];
	const out = await api.fetchHistoryDay(
		'2026-11-13',
		fakeFetch(calls, 200, NIGHT),
		'Europe/London'
	);
	assert.equal(calls[0].url, `${API_BASE}/api/v1/history/2026-11-13?tz=Europe%2FLondon`);
	assert.equal(out.plays.length, 3);
});

test('Save as playlist POSTs the night and returns the new playlist', async () => {
	const calls = [];
	const saved = {
		playlist_id: 'pl-9',
		name: 'History 2026-11-13',
		track_count: 2,
		skipped: ['gone']
	};
	const out = await api.saveHistoryDayAsPlaylist('2026-11-13', null, fakeFetch(calls, 201, saved));
	assert.equal(calls[0].url, `${API_BASE}/api/v1/history/2026-11-13/playlist`);
	assert.equal(calls[0].method, 'POST');
	assert.equal(calls[0].body, '{}');
	assert.equal(out.playlist_id, 'pl-9');
	assert.equal(
		view.historySavedMessage(out),
		'Saved playlist "History 2026-11-13" (2 tracks, 1 no longer in the collection left out)'
	);
});

test('a refused save surfaces the server error instead of a fake success', async () => {
	const detail = { detail: { code: 'NO_PLAYS', message: 'no plays logged on 2026-11-12' } };
	await assert.rejects(() =>
		api.saveHistoryDayAsPlaylist('2026-11-12', null, fakeFetch([], 404, detail))
	);
});

test('play rows keep play order, carry the local time played, and mark tracks gone from the collection', () => {
	const rows = view.historyPlayRows(NIGHT.plays, TRACKS, 'UTC');
	assert.deepEqual(
		rows.map((row) => row.position),
		[1, 2, 3]
	);
	assert.deepEqual(
		rows.map((row) => row.time),
		['22:05', '23:59', '00:40']
	);
	assert.equal(rows[0].title, 'Strings of Life');
	assert.equal(rows[1].gone, true);
	assert.equal(rows[2].play_id, null);
});

test('a night label carries its weekday, and the open night closes once it has no plays', () => {
	assert.equal(view.historyNightLabel('2026-11-13'), 'Fri 2026-11-13');
	assert.equal(view.keepOpenNight('2026-11-13', DAYS.days), '2026-11-13');
	assert.equal(view.keepOpenNight('2026-11-12', DAYS.days), null);
});

test('the nights list renders newest first with play counts', () => {
	const html = renderPanel({ days: DAYS.days });
	const dates = [...html.matchAll(/data-testid="history-night" data-date="([^"]+)"/g)].map(
		(m) => m[1]
	);
	assert.deepEqual(dates, ['2026-11-13', '2026-11-06']);
	assert.match(html, /3 plays/);
	assert.match(html, /1 play</);
});

test('an open night renders its plays in order with Played at times and an enabled Save as playlist', () => {
	const plays = view.historyPlayRows(NIGHT.plays, TRACKS, 'UTC');
	const html = renderPanel({ days: DAYS.days, night: '2026-11-13', plays });
	assert.match(html, /data-testid="history-night-title"[^>]*>Fri 2026-11-13</);
	const ids = [...html.matchAll(/data-testid="history-play" data-stable-id="([^"]+)"/g)].map(
		(m) => m[1]
	);
	assert.deepEqual(ids, ['a', 'gone', 'a']);
	assert.match(html, /title="Played at">22:05</);
	const save = /<button\b[^>]*data-testid="history-save-playlist"[^>]*>/.exec(html)?.[0];
	assert.ok(save);
	assert.doesNotMatch(
		save,
		/\sdisabled/,
		'if Save as playlist is disabled on a night with plays then broken'
	);
});

test('an empty history says so, and a load failure is shown, not hidden', () => {
	assert.match(renderPanel({}), /No plays logged yet/);
	assert.match(
		renderPanel({ error: 'GET /history 500' }),
		/History failed to load: GET \/history 500/
	);
	const empty = renderPanel({ night: '2026-11-12' });
	assert.match(
		/<button\b[^>]*data-testid="history-save-playlist"[^>]*>/.exec(empty)[0],
		/\sdisabled/
	);
});
