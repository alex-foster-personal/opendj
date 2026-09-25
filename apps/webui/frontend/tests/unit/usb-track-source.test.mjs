import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Play from USB, lane D (specs/usb-play-from-stick.md 4b "Routing" and
 * "Frontend guards"): one helper swaps a stick id onto /api/v1/usb/tracks,
 * and every write for a stick id is refused before any request.
 *
 * Regression lines:
 * - if isUsbTrackId accepts a library sha1 id then a library deck loads from the usb routes and 404s
 * - if trackApiPath stops encoding the id then an id with a slash or query char addresses another route
 * - if audioUrl / fetchAnlz / fetchHotCueSlots / artworkUrl / getTrack keep the library prefix for a stick id then every stick load 404s
 * - if a hot cue write, patchTrack or a lyric override reaches fetch for a stick id then decision 2 (never written) is broken
 * - if fetchAnlz accepts a stick payload naming another id then the source-confirmation exemption keys on the wrong id
 */

const API_BASE = 'https://usb-track-source.example.test';
const STICK_ID = 'usb-C74521A7-BFC7-3382-B11A-FBE569767B61-36';
const STICK_ANLZ_QUERY = /^\/anlz\?/;

let source;
let rb;
let api;
let requests;
const originalFetch = globalThis.fetch;

function libraryId(material) {
	// The exact construction of apps/shared/state/ids.py::stable_id (every tier).
	return createHash('sha1').update(material).digest('hex');
}

function recordingFetch(respond) {
	return async (input, init) => {
		const url = input instanceof Request ? input.url : String(input);
		const method = input instanceof Request ? input.method : (init?.method ?? 'GET');
		requests.push({ url, method });
		return respond(url, method);
	};
}

function json(body, status = 200, headers = {}) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json', ...headers }
	});
}

function stickAnlz(stable_id) {
	return {
		stable_id,
		points: 1200,
		waveform: { kind: 'mono', preview: { length: 0, low: [], mid: [], high: [] }, detail: { length: 0, low: [], mid: [], high: [] } },
		beatgrid: { source: 'rekordbox', beat_count: 0, beats: [] },
		beatgrid_source: 'rekordbox',
		cues: [],
		phrases: [],
		vocals: { status: 'not_analyzed' }
	};
}

before(async () => {
	source = await loadTypeScriptModule('src/lib/rb/track-source.ts');
	rb = await loadTypeScriptModule('src/lib/rb/api-rb.ts', { viteApiBase: API_BASE });
	api = await loadTypeScriptModule('src/lib/api.ts', { viteApiBase: API_BASE });
});

beforeEach(() => {
	requests = [];
	globalThis.fetch = recordingFetch(() => {
		throw new Error('no request expected in this test');
	});
});

after(() => {
	globalThis.fetch = originalFetch;
});

//-----------------------------------------------------------------------------
// the helper itself
//-----------------------------------------------------------------------------

test('[if] an id is a stick id [then] isUsbTrackId says so [else stick loads hit the library routes]', () => {
	assert.equal(source.isUsbTrackId(STICK_ID), true);
	assert.equal(source.isUsbTrackId('usb-anything'), true, 'the prefix is the discriminator; the backend validates the rest');
});

test('[if] an id is a library sha1 id [then] isUsbTrackId rejects it [else library decks route to the stick]', () => {
	for (const material of ['GBUM71029604', 'fp|240000|9400112', '/Users/dev/Music/a.mp3|1.5', '']) {
		const id = libraryId(material);
		assert.match(id, /^[0-9a-f]{40}$/, 'control: the library construction is 40 lowercase hex chars');
		assert.equal(source.isUsbTrackId(id), false, `library id ${id} read as a stick id`);
	}
	for (const notStick of ['', 'usb', 'USB-C745', 'xusb-1', ' usb-1', 'vol:C745']) {
		assert.equal(source.isUsbTrackId(notStick), false, `${JSON.stringify(notStick)} read as a stick id`);
	}
});

test('[if] trackApiPath builds a path [then] stick ids get the usb prefix and library ids keep theirs', () => {
	const lib = libraryId('GBUM71029604');
	assert.equal(source.trackApiPath(STICK_ID, '/audio'), `/api/v1/usb/tracks/${STICK_ID}/audio`);
	assert.equal(source.trackApiPath(lib, '/audio'), `/api/v1/tracks/${lib}/audio`);
	assert.equal(source.trackApiPath(STICK_ID), `/api/v1/usb/tracks/${STICK_ID}`, 'suffix defaults to empty');
	assert.equal(
		source.trackApiPath(STICK_ID, '/anlz?points=1200&gen=3'),
		`/api/v1/usb/tracks/${STICK_ID}/anlz?points=1200&gen=3`,
		'the suffix is appended verbatim, never encoded'
	);
});

test('[if] an id carries URL syntax [then] trackApiPath encodes it [else it addresses another route]', () => {
	assert.equal(source.trackApiPath('usb-a/../b?x', '/audio'), '/api/v1/usb/tracks/usb-a%2F..%2Fb%3Fx/audio');
	assert.equal(source.trackApiPath('track / one'), '/api/v1/tracks/track%20%2F%20one');
});

test('[if] session ids mix stick and library ids [then] withoutUsbTrackIds keeps only library ids, in order', () => {
	const a = libraryId('a');
	const b = libraryId('b');
	assert.deepEqual(source.withoutUsbTrackIds([a, STICK_ID, b, 'usb-X-1']), [a, b]);
	assert.deepEqual(source.withoutUsbTrackIds([]), []);
});

test('UsbTrackRefusal is a typed RbApiError with no HTTP status', () => {
	const refusal = new source.UsbTrackRefusal('USB_READ_ONLY', STICK_ID, 'hot cue A save');
	assert.equal(refusal.code, 'USB_READ_ONLY');
	assert.equal(refusal.status, 0);
	assert.equal(refusal.name, 'UsbTrackRefusal');
	assert.match(refusal.message, /USB_READ_ONLY: hot cue A save refused for stick track usb-/);
});

test('[if] refuseStickWrite / refuseStickRead see a stick id [then] they throw their code, and a library id passes through', () => {
	assert.throws(() => source.refuseStickWrite(STICK_ID, 'track edit'), (error) => error.code === 'USB_READ_ONLY');
	assert.throws(() => source.refuseStickRead(STICK_ID, 'reveal'), (error) => error.code === 'USB_NOT_SUPPORTED');
	assert.equal(source.refuseStickWrite(libraryId('w'), 'track edit'), undefined);
	assert.equal(source.refuseStickRead(libraryId('r'), 'reveal'), undefined);
});

//-----------------------------------------------------------------------------
// api-rb.ts routing
//-----------------------------------------------------------------------------

test('[if] a stick id is loaded [then] audio and artwork URLs use the usb routes', () => {
	assert.equal(rb.audioUrl(STICK_ID), `${API_BASE}/api/v1/usb/tracks/${STICK_ID}/audio`);
	assert.equal(rb.artworkUrl(STICK_ID, 'm'), `${API_BASE}/api/v1/usb/tracks/${STICK_ID}/artwork?size=m`);
	const lib = libraryId('x');
	assert.equal(rb.audioUrl(lib), `${API_BASE}/api/v1/tracks/${lib}/audio`, 'control: library ids unchanged');
	assert.equal(rb.artworkUrl(lib), `${API_BASE}/api/v1/tracks/${lib}/artwork?size=s`);
});

test('[if] a stick deck loads [then] anlz (both variants) and hot cues are fetched from the usb routes', async () => {
	globalThis.fetch = recordingFetch((url) =>
		url.includes('/hot-cues') ? json([]) : json(stickAnlz(STICK_ID))
	);
	await rb.fetchAnlz(STICK_ID, 1200, true);
	await rb.fetchAnlzBypassingHttpCache(STICK_ID, 1200);
	await rb.fetchHotCueSlots(STICK_ID);
	const prefix = `${API_BASE}/api/v1/usb/tracks/${STICK_ID}`;
	assert.equal(requests.length, 3);
	assert.match(requests[0].url.slice(prefix.length), STICK_ANLZ_QUERY);
	assert.ok(requests[0].url.startsWith(prefix), requests[0].url);
	assert.ok(requests[1].url.startsWith(`${prefix}/anlz?`), requests[1].url);
	assert.equal(requests[2].url, `${prefix}/hot-cues`);
});

test('[if] a stick /anlz answers for a different id [then] fetchAnlz rejects [else the source exemption keys on the wrong id]', async () => {
	globalThis.fetch = recordingFetch(() => json(stickAnlz('usb-OTHER-1')));
	await assert.rejects(rb.fetchAnlz(STICK_ID, 1200, true), /answered for usb-OTHER-1/);
	await assert.rejects(rb.fetchAnlzBypassingHttpCache(STICK_ID, 1200), /answered for usb-OTHER-1/);
	// Control: a library payload is not held to this (its id is the library's own).
	const lib = libraryId('y');
	globalThis.fetch = recordingFetch(() => json(stickAnlz('abc')));
	await rb.fetchAnlz(lib, 1200, true);
});

// The session behavior itself (revisions, reversals, reads that carry the
// edit) is covered in usb-stick-session-edits.test.mjs; this pins only that a
// stick edit never reaches fetch, even when it cannot proceed.
test('[if] a stick deck edits a hot cue before its slots were read [then] it fails fast with no request', async () => {
	for (const [call, expected] of [
		[() => rb.saveHotCue(STICK_ID, 'A', 1000, 'rev'), /were never read/],
		[() => rb.clearHotCue(STICK_ID, 'A', 'rev'), /were never read/],
		[() => rb.restoreHotCue(STICK_ID, 'A', 'rev', 'token'), /HOT_CUE_REVERSAL_NOT_FOUND/]
	]) {
		await assert.rejects(call(), expected);
	}
	assert.equal(requests.length, 0);
});

test('[if] a library deck edits a hot cue [then] the write still reaches the library route (control)', async () => {
	const lib = libraryId('z');
	globalThis.fetch = recordingFetch(() => json({ cue: null, revision: 'r2', reversal: { reversal_id: 't' } }));
	await rb.clearHotCue(lib, 'B', 'rev');
	assert.deepEqual(requests, [{ url: `${API_BASE}/api/v1/tracks/${lib}/hot-cues/B`, method: 'DELETE' }]);
});

test('[if] a stick id asks for library-only reads [then] they settle without a request', async () => {
	assert.equal(await rb.fetchTrackLyrics(STICK_ID), null);
	assert.deepEqual(await rb.probeStemArtifact(STICK_ID), {
		status: 'unavailable',
		error: 'stick tracks have no stem bundle'
	});
	await assert.rejects(rb.fetchRbMeta(STICK_ID), (error) => error.code === 'USB_NOT_SUPPORTED');
	assert.throws(() => rb.stemAudioUrl(STICK_ID, 'vocals'), (error) => error.code === 'USB_NOT_SUPPORTED');
	assert.equal(requests.length, 0);
});

//-----------------------------------------------------------------------------
// api.ts
//-----------------------------------------------------------------------------

test('[if] a stick deck loads [then] getTrack reads the usb TrackOut and remembers its optional-resource flags', async () => {
	globalThis.fetch = recordingFetch(() =>
		json(
			{
				stable_id: STICK_ID,
				title: 'T',
				has_rb_mapping: false,
				lyrics_available: false,
				auto_cues_available: false,
				stems_available: false,
				artwork_available: true,
				created_at: '2026-09-25T00:00:00Z',
				updated_at: '2026-09-25T00:00:00Z'
			},
			200,
			{ etag: 'W/"s"' }
		)
	);
	const { track, etag } = await api.getTrack(STICK_ID);
	assert.equal(track.stable_id, STICK_ID);
	assert.equal(etag, 'W/"s"');
	assert.deepEqual(requests, [{ url: `${API_BASE}/api/v1/usb/tracks/${STICK_ID}`, method: 'GET' }]);
	// The store is a globalThis singleton (optional-resource-availability.ts),
	// so it is readable here even though api.ts was bundled on its own. These
	// flags are what keep lyrics, auto-cue and stem fetches off a stick deck.
	assert.deepEqual(globalThis.__musicDjToolsOptionalResourceStore.get(STICK_ID), {
		lyrics: false,
		autoCues: false,
		stems: false,
		artwork: true
	});
});

test('[if] the stick is unplugged [then] getTrack throws ApiError carrying USB_STICK_NOT_MOUNTED', async () => {
	globalThis.fetch = recordingFetch(() =>
		json({ detail: { code: 'USB_STICK_NOT_MOUNTED', message: 'stick not mounted', volume_uuid: 'C745' } }, 404)
	);
	await assert.rejects(api.getTrack(STICK_ID), (error) => {
		assert.equal(error.name, 'ApiError');
		assert.equal(error.status, 404);
		assert.equal(error.code, 'USB_STICK_NOT_MOUNTED');
		return true;
	});
});

test('[if] a library id is read [then] getTrack keeps the typed library route (control)', async () => {
	const lib = libraryId('lib');
	globalThis.fetch = recordingFetch(() =>
		json({ stable_id: lib, has_rb_mapping: true, created_at: 'x', updated_at: 'x' })
	);
	await api.getTrack(lib);
	assert.equal(requests[0].url, `${API_BASE}/api/v1/tracks/${lib}`);
});

test('[if] a stick track is edited [then] patchTrack and the lyric override refuse USB_READ_ONLY with no request', async () => {
	await assert.rejects(api.patchTrack(STICK_ID, 'etag', { rating: 5 }), (error) => error.code === 'USB_READ_ONLY');
	await assert.rejects(api.putLyricOverride(STICK_ID, 'vocal'), (error) => error.code === 'USB_READ_ONLY');
	assert.equal(await api.getTrackLyricsWords(STICK_ID), null);
	assert.equal(requests.length, 0);
});
