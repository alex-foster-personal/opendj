/**
 * P0 memory leak, Open DJ 1.0 alpha (Tue 6 Oct 2026): every stem download
 * gets its OWN abort signal, released when the download ends.
 *
 * WHY THIS FILE EXISTS. 935752ef33 (Sat 26 Sep 2026) passed ONE page-lifetime
 * AbortController's signal to every stem fetch. WebKit keeps each fetch's
 * following signal (and its Response) alive while the source signal lives, so
 * the packaged build's heap with all decks EMPTY held 2,951 AbortSignal, 733
 * signalFollow and 2,243 FetchResponse objects.
 *
 * [if] two stem downloads share one signal [then ⛔] the shared source retains
 *   every download of the session.
 * [if] a finished download stays registered [then ⛔] the registry itself leaks.
 * [if] a non-OK stem part, or a playlist page with no ETag, throws without
 *   reading its body [then ⛔] each failed request leaks its Response (Sol
 *   review rounds on #5703).
 * [if] a block mid-download does not abort it [then ⛔] Trackify downloads
 *   stems again (control, PERFMODE-15; trackify-no-stems.test.mjs covers it
 *   end to end through the engine).
 */
import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';

import { importBundledSource } from './import-bundled-source.mjs';
import { bundleTypeScriptModule } from './load-typescript.mjs';

const bundleText = await bundleTypeScriptModule('tests/unit/fixtures/stem-work-signal-entry.ts', {
	dev: true
});
let entry;
let realFetch;
let signals;

beforeEach(async () => {
	entry = await importBundledSource(bundleText, 'stem-work-signal-entry');
	realFetch = globalThis.fetch;
	signals = [];
});

afterEach(() => {
	globalThis.fetch = realFetch;
});

test('completed stem downloads each get a different signal and leave none registered', async () => {
	globalThis.fetch = async (_url, init) => {
		signals.push(init?.signal);
		return new Response(new Uint8Array([1, 2, 3]), { status: 200 });
	};
	await entry.fetchStemAudioArrayBuffers('track-a', 'demucs4');
	await entry.fetchStemAudioArrayBuffers('track-b', 'demucs4');
	assert.equal(signals.length, 8, 'four parts per load, two loads');
	assert.ok(signals.every((signal) => signal instanceof AbortSignal && !signal.aborted));
	assert.equal(new Set(signals).size, signals.length, 'no two stem fetches share a signal');
	assert.equal(entry.inFlightStemWorkCount(), 0, 'every finished download is unregistered');
});

test('a failed stem download is unregistered too', async () => {
	globalThis.fetch = async () => {
		throw new TypeError('Load failed');
	};
	await assert.rejects(entry.fetchStemAudioArrayBuffers('track-a', 'demucs4'));
	assert.equal(entry.inFlightStemWorkCount(), 0);
});

test('control: a block mid-download still aborts every running part', async () => {
	globalThis.fetch = (_url, init) => {
		signals.push(init?.signal);
		return new Promise((_resolve, reject) => {
			init.signal.addEventListener('abort', () => reject(init.signal.reason), { once: true });
		});
	};
	const download = entry.fetchStemAudioArrayBuffers('track-a', 'demucs4');
	for (let i = 0; i < 20 && signals.length < 4; i += 1) await new Promise((r) => setImmediate(r));
	assert.equal(entry.inFlightStemWorkCount(), 4, 'all four parts registered while running');
	const release = entry.blockStemDecode('trackify');
	await assert.rejects(download, (error) => error.name === 'AbortError');
	assert.ok(signals.every((signal) => signal.aborted));
	assert.equal(entry.inFlightStemWorkCount(), 0);
	signals = [];
	globalThis.fetch = async (_url, init) => {
		signals.push(init?.signal);
		throw init.signal.reason;
	};
	await assert.rejects(entry.fetchStemAudioArrayBuffers('track-b', 'demucs4'));
	assert.ok(signals.length === 0 || signals.every((s) => s.aborted), 'blocked: born aborted');
	release();
});

/** A Response whose body records whether it was cancelled or fully read. */
function trackedResponse(status, text, headers = {}) {
	// Never closes by itself (highWaterMark 0): only a read or a cancel releases it.
	const record = { cancelled: false, read: false, response: null };
	const stream = new ReadableStream(
		{
			pull(controller) {
				controller.enqueue(new TextEncoder().encode(text));
				controller.close();
				record.read = true;
			},
			cancel() {
				record.cancelled = true;
			}
		},
		{ highWaterMark: 0 }
	);
	record.response = new Response(stream, { status, headers });
	return record;
}
const released = (record) => record.cancelled || record.read;

test('a non-OK stem part throws with its body released (and its message kept)', async () => {
	const records = [];
	globalThis.fetch = async () => {
		const record = trackedResponse(404, JSON.stringify({ detail: { code: 'STEM_PART_NOT_FOUND', message: 'no part' } }));
		records.push(record);
		return record.response;
	};
	await assert.rejects(entry.fetchStemAudioArrayBuffers('track-a', 'demucs4'), /no part/);
	assert.equal(records.length, 4);
	assert.ok(records.every(released), 'every non-OK stem body was read or cancelled');
	assert.equal(entry.inFlightStemWorkCount(), 0);
});

test('a playlist page with no ETag throws with its body released', async () => {
	let record;
	globalThis.fetch = async () => {
		record = trackedResponse(200, JSON.stringify({ tracks: [], total: 0 }), { 'content-type': 'application/json' });
		return record.response;
	};
	await assert.rejects(entry.listPlaylistTracksPage('pl-1', { offset: 0, limit: 10 }), /no ETag/);
	await new Promise((resolve) => setImmediate(resolve));
	assert.ok(released(record), 'the unread page body was cancelled');
});
