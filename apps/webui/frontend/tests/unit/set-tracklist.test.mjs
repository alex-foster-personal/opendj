import assert from 'node:assert/strict';
import fs from 'node:fs';
import { after, before, test } from 'node:test';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://sets-api.example.test';
const ENCODED_SESSION = encodeURIComponent('s 1');
const TRACKLIST_SVELTE = fileURLToPath(
	new URL('../../src/routes/sets/SetTracklist.svelte', import.meta.url)
);
const SSR_ENTRY = `export { default as SetTracklist } from ${JSON.stringify(TRACKLIST_SVELTE)};\nexport { render } from 'svelte/server';`;

const FIXTURE_ROWS = [
	{
		position: 1,
		start_s: 0,
		start_label: '00:00',
		artist: 'A1',
		title: 'One',
		display_name: 'A1 - One',
		key: '8A',
		played_s: 60,
		played_label: '1:00',
		transition_label: 'blend',
		hidden: false,
		track_stable_id: 'a',
		source: 'opendj',
		deck: 'A',
		duration_ms: 240000
	},
	{
		position: 2,
		start_s: 60,
		start_label: '01:00',
		artist: 'ID',
		title: 'ID',
		display_name: 'ID - ID',
		key: null,
		played_s: 120,
		played_label: '2:00',
		transition_label: 'cut',
		hidden: true,
		track_stable_id: 'b',
		source: 'opendj',
		deck: 'B',
		duration_ms: null
	},
	{
		position: 3,
		start_s: 180,
		start_label: '03:00',
		artist: 'A3',
		title: 'Three',
		display_name: 'A3 - Three',
		key: null,
		played_s: 90,
		played_label: '1:30',
		transition_label: null,
		hidden: false,
		track_stable_id: 'c',
		source: 'opendj',
		deck: 'A',
		duration_ms: null
	}
];

let ssr;
let tracklistModule;
let api;
let originalFetch;
let requests;

before(async () => {
	ssr = await loadSvelteSsrModule(SSR_ENTRY);
	tracklistModule = await loadTypeScriptModule('src/routes/sets/set-tracklist.ts', {
		viteApiBase: API_BASE
	});
	api = await loadTypeScriptModule('src/routes/sets/sets-api.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
	requests = [];
	globalThis.fetch = async (request) => {
		const body = request.body === null ? null : await request.clone().text();
		requests.push({ url: request.url, method: request.method, body });
		return new Response('tracklist body', {
			status: 200,
			headers: { 'content-type': 'text/plain' }
		});
	};
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('SetTracklist SSR renders table rows and controls', () => {
	const html = ssr.render(ssr.SetTracklist, {
		props: {
			sessionId: 's1',
			rows: FIXTURE_ROWS,
			soundcloudReady: false,
			oncopy: () => {},
			ondownload: () => {}
		}
	}).body;
	const trCount = (html.match(/<tbody[^>]*>[\s\S]*?<\/tbody>/i)?.[0].match(/<tr/g) ?? []).length;
	assert.equal(trCount, 3);
	assert.equal((html.match(/<th[^>]*scope="col"/g) ?? []).length, 7);
	assert.match(html, />\s*00:00\s*</);
	assert.match(html, />\s*01:00\s*</);
	assert.match(html, />\s*03:00\s*</);
	assert.match(html, />\s*8A\s*</);
	assert.match(html, />\s*1:00\s*</);
	assert.match(html, />\s*2:00\s*</);
	assert.match(html, />\s*1:30\s*</);
	assert.match(html, /blend/);
	assert.match(html, /cut/);
	assert.match(html, />\s*ID\s*</);
	assert.match(html, /Hidden from tracklists/);
	const tbody = html.match(/<tbody[^>]*>[\s\S]*?<\/tbody>/i)?.[0] ?? '';
	const rows = tbody.split('<tr>').slice(1);
	assert.doesNotMatch(rows[2] ?? '', /blend|cut/);
	assert.equal((html.match(/<button type="button"/g) ?? []).length, 7);
	assert.match(html, /disabled/);
});

test('SetTracklist empty state', () => {
	const html = ssr.render(ssr.SetTracklist, {
		props: {
			sessionId: 's1',
			rows: [],
			soundcloudReady: false,
			oncopy: () => {},
			ondownload: () => {}
		}
	}).body;
	assert.match(html, /No kept plays in this session/);
	assert.doesNotMatch(html, /<table/i);
	assert.doesNotMatch(html, /<button/i);
});

test('copyTracklistAs fetches once per format', async () => {
	requests.length = 0;
	const clipboard = [];
	const notifies = [];
	for (const item of tracklistModule.COPY_FORMATS) {
		await tracklistModule.copyTracklistAs('s 1', item.format, {
			fetchText: api.getTracklistText,
			writeClipboard: async (text) => {
				clipboard.push(text);
			},
			notify: (message, kind) => notifies.push({ message, kind })
		});
	}
	assert.equal(requests.length, 5);
	assert.equal(clipboard.length, 5);
	for (const item of tracklistModule.COPY_FORMATS) {
		const expected = `${API_BASE}/api/sets/${ENCODED_SESSION}/tracklist.${item.format}`;
		const hits = requests.filter((r) => r.url.startsWith(expected));
		assert.equal(hits.length, 1, item.format);
	}
	for (const text of clipboard) {
		assert.equal(text, 'tracklist body');
	}
	const soundcloud = requests.find((r) => r.url.includes('soundcloud'));
	assert.ok(soundcloud?.url.includes('acknowledge_rights=true'));
});

test('copyTracklistAs failure toast and no clipboard write', async () => {
	const prev = globalThis.fetch;
	globalThis.fetch = async () => new Response('nope', { status: 500 });
	const clipboard = [];
	const notifies = [];
	const ok = await tracklistModule.copyTracklistAs('s1', 'txt', {
		fetchText: api.getTracklistText,
		writeClipboard: async (text) => {
			clipboard.push(text);
		},
		notify: (message, kind) => notifies.push({ message, kind })
	});
	globalThis.fetch = prev;
	assert.equal(ok, false);
	assert.equal(clipboard.length, 0);
	assert.equal(notifies.length, 1);
	assert.equal(notifies[0].kind, 'error');
});

test('downloadTracklist triggers anchor click', () => {
	const anchors = [];
	const fakeDoc = {
		body: { appendChild() {}, removeChild() {} },
		createElement() {
			const anchor = {
				href: '',
				download: '',
				rel: '',
				click() {},
				remove() {}
			};
			anchors.push(anchor);
			return anchor;
		}
	};
	tracklistModule.downloadTracklist('s1', 'cue', fakeDoc);
	tracklistModule.downloadTracklist('s1', 'm3u8', fakeDoc);
	assert.equal(anchors.length, 2);
	assert.equal(anchors[0].download, 's1.cue');
	assert.equal(anchors[1].download, 's1.m3u8');
	assert.ok(anchors[0].href.endsWith('/api/sets/s1/tracklist.cue'));
	assert.ok(anchors[1].href.endsWith('/api/sets/s1/tracklist.m3u8'));
});

test('+page.svelte wires SetTracklist', () => {
	const pagePath = path.join(process.cwd(), 'src/routes/sets/+page.svelte');
	const source = fs.readFileSync(pagePath, 'utf8');
	assert.match(source, /SetTracklist/);
	assert.match(source, /soundcloudReady=\{exportAcknowledged\}/);
});
