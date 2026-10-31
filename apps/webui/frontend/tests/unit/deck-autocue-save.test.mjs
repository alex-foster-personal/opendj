// DECKCTL-6235 / Codex P2 on PR #6235: the deck_autocue command confirms only a
// saved choice. setAutoCue resolves after the verified ui-prefs PUT lands, and
// on a failed save it rejects and puts the previous value back, unless a newer
// choice replaced it meanwhile.
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://deck-autocue-save.example.test';

let prefs;
let originalFetch;
// PUTs go to `put` and a 2xx one lands on `disk`; a GET (the re-read)
// answers `external` when another client wrote after that, else `disk`.
let put;
let disk;
let external;
let readsDown;

function jsonResponse(body, status = 200) {
	return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

before(async () => {
	prefs = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
	globalThis.fetch = async (request) => {
		if (request.method === 'GET') {
			if (readsDown) throw new TypeError('Failed to fetch');
			return jsonResponse({ deck_autocue: external ?? disk });
		}
		const body = await request.clone().json();
		const response = await put(request);
		if (response.ok) disk = body.deck_autocue;
		return response;
	};
});

after(() => {
	globalThis.fetch = originalFetch;
});

beforeEach(() => {
	prefs.uiPrefs.deck_autocue = false;
	disk = false;
	external = undefined;
	readsDown = false;
});

test('setAutoCue resolves after a 2xx PUT and keeps the new value', async () => {
	let body;
	put = async (request) => {
		body = await request.clone().json();
		return jsonResponse({ deck_autocue: true });
	};
	await prefs.setAutoCue(true);
	assert.deepEqual(body, { deck_autocue: true });
	assert.equal(prefs.uiPrefs.deck_autocue, true);
});

test('setAutoCue rejects on an HTTP error and puts the previous value back', async () => {
	put = async () => jsonResponse({ detail: { code: 'WRITE_FAILED', message: 'disk full' } }, 500);
	await assert.rejects(prefs.setAutoCue(true), /disk full/);
	assert.equal(prefs.uiPrefs.deck_autocue, false);
});

test('setAutoCue rejects on a transport failure and puts the previous value back', async () => {
	put = async () => {
		throw new TypeError('Failed to fetch');
	};
	await assert.rejects(prefs.setAutoCue(true), /Failed to fetch/);
	assert.equal(prefs.uiPrefs.deck_autocue, false);
});

test('a failed save does not undo a newer choice queued behind it', async () => {
	const calls = [];
	put = (request) =>
		new Promise((resolve, reject) => {
			calls.push({ request, resolve, reject });
		});
	const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
	const first = prefs.setAutoCue(true);
	const second = prefs.setAutoCue(false);
	const third = prefs.setAutoCue(true);
	await tick();
	calls[0].reject(new TypeError('Failed to fetch'));
	await assert.rejects(first, /Failed to fetch/);
	// The third choice equals the failed one, but it is newer and still saving.
	assert.equal(prefs.uiPrefs.deck_autocue, true);
	for (let i = 1; i <= 2; i += 1) {
		await tick();
		calls[i].resolve(jsonResponse({}));
	}
	await second;
	await third;
	assert.equal(prefs.uiPrefs.deck_autocue, true);
	assert.deepEqual(await calls[2].request.clone().json(), { deck_autocue: true });
});

test('two queued choices that both fail leave the value disk still holds', async () => {
	// Codex P2 BLOCKING on PR #6235: the newest failure must not roll back to
	// the older failed choice it was queued behind.
	put = async () => {
		throw new TypeError('Failed to fetch');
	};
	const first = prefs.setAutoCue(true);
	const second = prefs.setAutoCue(false);
	await assert.rejects(first);
	await assert.rejects(second);
	assert.equal(prefs.uiPrefs.deck_autocue, false);
	put = async () => {
		throw new TypeError('Failed to fetch');
	};
	prefs.uiPrefs.deck_autocue = true;
	disk = true;
	const third = prefs.setAutoCue(false);
	const fourth = prefs.setAutoCue(true);
	await assert.rejects(third);
	await assert.rejects(fourth);
	assert.equal(prefs.uiPrefs.deck_autocue, true, 'mirror case: starting from on');
});

test('a failed choice queued behind a saved one goes back to the saved one', async () => {
	// Control for the overshoot: the rollback target moves when a save lands.
	let calls = 0;
	put = async () => {
		calls += 1;
		if (calls === 1) return jsonResponse({});
		throw new TypeError('Failed to fetch');
	};
	const first = prefs.setAutoCue(true);
	const second = prefs.setAutoCue(false);
	await first;
	await assert.rejects(second);
	assert.equal(prefs.uiPrefs.deck_autocue, true);
});

test('a failed save re-reads disk, so a value another client saved meanwhile shows', async () => {
	// Codex P2 BLOCKING on PR #6235: the ui_prefs event for the other client's
	// save was consumed while this save was pending, so only a re-read heals.
	put = async () => {
		throw new TypeError('Failed to fetch');
	};
	disk = true;
	await assert.rejects(prefs.setAutoCue(true));
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.equal(prefs.uiPrefs.deck_autocue, true);
});

test('a saved newest choice shows even if a re-read replaced it while the PUT was pending', async () => {
	// Codex P2 BLOCKING on PR #6235: a ui_prefs refresh that read disk before
	// this PUT landed puts the old value back; the landed choice must win.
	let land;
	put = () => new Promise((resolve) => (land = () => resolve(jsonResponse({}))));
	const pending = prefs.setAutoCue(true);
	await new Promise((resolve) => setTimeout(resolve, 0));
	prefs.uiPrefs.deck_autocue = false; // what that refresh wrote
	land();
	await pending;
	assert.equal(prefs.uiPrefs.deck_autocue, true);
});

test('a saved older choice does not override a newer one still pending', async () => {
	// Control for the overshoot: only the newest choice reapplies itself.
	const lands = [];
	put = () => new Promise((resolve) => lands.push(() => resolve(jsonResponse({}))));
	const first = prefs.setAutoCue(true);
	const second = prefs.setAutoCue(false);
	await new Promise((resolve) => setTimeout(resolve, 0));
	lands[0]();
	await first;
	assert.equal(prefs.uiPrefs.deck_autocue, false);
	await new Promise((resolve) => setTimeout(resolve, 0));
	lands[1]();
	await second;
	assert.equal(prefs.uiPrefs.deck_autocue, false);
});

test('a write another client made after this PUT landed is what shows', async () => {
	// Codex P2 BLOCKING on PR #6235: that client's event may already be
	// consumed, so the newest choice re-reads disk before it confirms.
	put = async () => {
		external = false; // the other client's write lands right after this one
		return jsonResponse({});
	};
	await prefs.setAutoCue(true);
	assert.equal(prefs.uiPrefs.deck_autocue, false);
});

test('the command fails when the save landed but the confirming re-read never did', async () => {
	// Codex P2 BLOCKING on PR #6235: the refresher reports and resolves on a
	// terminal read failure, so the command path checks its result.
	put = async () => jsonResponse({});
	readsDown = true;
	await assert.rejects(prefs.setAutoCue(true), /could not re-read it to confirm/);
	assert.equal(disk, true, 'the save itself landed');
	assert.equal(prefs.uiPrefs.deck_autocue, true, 'and is not rolled back');
});
