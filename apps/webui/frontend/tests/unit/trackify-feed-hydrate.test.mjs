/**
 * Tests for `trackify-feed.svelte.ts`'s reactive hydrate cycle (PERFMODE-15).
 *
 * `trackify-feed.test.mjs` covers the plain `trackify-feed.ts` core (scope
 * derivation, pagination). This file covers the `.svelte.ts` layer's own
 * `_hydrate()`, which reads `uiPrefs.last_playlist` twice around an `await`
 * -- once to build the fetch, once again (implicitly, via the module-level
 * preference) to publish the result. A playlist switch mid-fetch must not
 * let rows fetched for the OLD selection land under the NEW one.
 */
import assert from 'node:assert/strict';
import { afterEach, beforeEach, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://trackify-feed-hydrate.example.test';

function jsonResponse(body) {
	return new Response(JSON.stringify(body), {
		status: 200,
		headers: { 'content-type': 'application/json' }
	});
}

function gate() {
	let release = () => {};
	const promise = new Promise((resolve) => {
		release = resolve;
	});
	return { promise, release };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

describe('trackify feed hydrate: playlist switch mid-fetch (PERFMODE-15)', () => {
	let originalFetch;
	let mod;
	let uninstall;

	beforeEach(async () => {
		originalFetch = globalThis.fetch;
		mod = await loadTypeScriptModule('tests/unit/fixtures/trackify-feed-entry.ts', {
			viteApiBase: API_BASE
		});
	});

	afterEach(() => {
		if (uninstall !== null && uninstall !== undefined) uninstall();
		uninstall = undefined;
		globalThis.fetch = originalFetch;
	});

	it('does not publish rows fetched for a playlist the operator has since switched away from', async () => {
		const held = gate();
		let fetchCount = 0;
		globalThis.fetch = async () => {
			fetchCount += 1;
			await held.promise;
			return jsonResponse({
				items: [
					{
						stable_id: 'all-tracks-row',
						key: '8A',
						bpm: 120,
						file_exists: true,
						file_availability: 'AVAILABILITY_PRESENT',
						has_rb_mapping: true
					}
				],
				next_cursor: null
			});
		};

		mod.uiPrefs.last_playlist = null; // all_tracks scope
		uninstall = mod.installTrackifyFeed();
		// Let the initial hydrate start and reach (and block on) the fetch.
		await settle();
		assert.equal(fetchCount, 1, 'the initial hydrate must have started its fetch');

		// The operator switches to a specific playlist WHILE the all_tracks
		// fetch above is still in flight.
		mod.uiPrefs.last_playlist = { playlist_id: 'p1', name: 'Warmup', kind: 'playlist' };

		// The stale all_tracks fetch now resolves.
		held.release();
		await settle();
		await settle();

		// The bug this guards against (Sol review, PR #3676): `_hydrate`
		// read `uiPrefs.last_playlist` a second time, AFTER the await, to
		// publish -- so rows fetched for the OLD scope (all_tracks) would be
		// stamped and merged as if they belonged to the NEW scope
		// (`playlist:p1`), corrupting PLAY-04 snapshot semantics for
		// whatever the operator is now looking at.
		assert.deepEqual(
			mod.getTrackifyFeedRows(),
			[],
			'a fetch resolved for a scope the operator has since left must not publish under the new scope'
		);
	});

	it('a scope switch mid-fetch schedules its own hydrate immediately, not after the 60 s interval (Sol review, PR #3676)', async () => {
		const held = gate();
		const fetchedUrls = [];
		let fetchCount = 0;
		globalThis.fetch = async (url) => {
			fetchCount += 1;
			fetchedUrls.push(String(url));
			if (fetchCount === 1) {
				await held.promise;
				return jsonResponse({
					items: [
						{
							stable_id: 'stale-row',
							key: '8A',
							bpm: 120,
							file_exists: true,
							file_availability: 'AVAILABILITY_PRESENT',
							has_rb_mapping: true
						}
					],
					next_cursor: null
				});
			}
			// The playlist-scope endpoint, unlike all_tracks, requires an ETag.
			return new Response(
				JSON.stringify({
					tracks: [{ stable_id: 'p1-row', key: '8A', bpm: 120, file_exists: true }],
					total: 1,
					next_offset: null
				}),
				{ status: 200, headers: { 'content-type': 'application/json', etag: 'p1-etag' } }
			);
		};

		mod.uiPrefs.last_playlist = null; // all_tracks scope
		uninstall = mod.installTrackifyFeed();
		await settle();
		assert.equal(fetchCount, 1, 'the initial hydrate must have started its fetch');

		// Switch scope while that fetch is still in flight, then let it
		// resolve and get discarded (already covered above).
		mod.uiPrefs.last_playlist = { playlist_id: 'p1', name: 'Warmup', kind: 'playlist' };
		held.release();
		await settle();
		await settle();

		// The bug this guards against (Sol review, PR #3676): discarding the
		// stale-scope response scheduled nothing for the newly selected
		// scope, so the feed would sit empty until the 60 s interval timer
		// fires. Without any further passage of time, a second fetch must
		// already have gone out for the new scope.
		assert.equal(
			fetchCount,
			2,
			'discarding a stale-scope response must immediately schedule a hydrate for the current scope'
		);
		assert.match(
			fetchedUrls[1],
			/playlists\/p1/,
			'the rescheduled hydrate must fetch the NEWLY selected scope, not repeat the old one'
		);

		// That fresh fetch resolves and DOES publish under the new scope.
		await settle();
		assert.deepEqual(mod.getTrackifyFeedRows().map((row) => row.stable_id), ['p1-row']);

		// Guard against the exact anomaly a prior attempt at this fix hit
		// (a mysterious unexplained THIRD fetch call): settle further and
		// confirm nothing else fires.
		for (let i = 0; i < 5; i += 1) await settle();
		assert.equal(fetchCount, 2, 'no further fetch should fire beyond the rescheduled one');
	});

	it('control: an unchanged playlist selection still publishes normally once its own fetch resolves', async () => {
		const held = gate();
		globalThis.fetch = async () => {
			await held.promise;
			return jsonResponse({
				items: [
					{
						stable_id: 'steady-row',
						key: '8A',
						bpm: 120,
						file_exists: true,
						file_availability: 'AVAILABILITY_PRESENT',
						has_rb_mapping: true
					}
				],
				next_cursor: null
			});
		};

		mod.uiPrefs.last_playlist = null;
		uninstall = mod.installTrackifyFeed();
		await settle();

		held.release();
		await settle();
		await settle();

		// Control for the overshoot direction: a fix that discards every
		// result (not just a stale one) would fail this, since nothing
		// changed the selection here.
		assert.deepEqual(mod.getTrackifyFeedRows().map((row) => row.stable_id), ['steady-row']);
	});

	it('a hydration in flight at teardown does not publish into the controller a remount resets (Sol review round 6)', async () => {
		const held = gate();
		let fetchCount = 0;
		globalThis.fetch = async () => {
			fetchCount += 1;
			await held.promise;
			return jsonResponse({
				items: [
					{
						stable_id: 'stale-row',
						key: '8A',
						bpm: 120,
						file_exists: true,
						file_availability: 'AVAILABILITY_PRESENT',
						has_rb_mapping: true
					}
				],
				next_cursor: null
			});
		};

		mod.uiPrefs.last_playlist = null;
		uninstall = mod.installTrackifyFeed();
		await settle();
		assert.equal(fetchCount, 1, 'the initial hydrate must have started its fetch');

		// Teardown fires WHILE the hydrate above is still in flight. The bug
		// this guards against (Sol review, PR #3676): the cleanup resets
		// `_controller` but neither cancels nor invalidates the pending
		// fetch, so it can later publish into the just-reset controller.
		uninstall();
		uninstall = undefined;

		held.release();
		await settle();
		await settle();

		assert.deepEqual(
			mod.getTrackifyFeedRows(),
			[],
			'a hydrate started before teardown must not publish into the controller teardown reset'
		);
	});

	it('a fresh install right after teardown gets its own hydrate, not denied by a stale in-flight flag (Sol review round 6)', async () => {
		const gate1 = gate();
		const gate2 = gate();
		let fetchCount = 0;
		globalThis.fetch = async () => {
			fetchCount += 1;
			const activeGate = fetchCount === 1 ? gate1 : gate2;
			await activeGate.promise;
			return jsonResponse({
				items: [
					{
						stable_id: fetchCount === 1 ? 'stale-row' : 'fresh-row',
						key: '8A',
						bpm: 120,
						file_exists: true,
						file_availability: 'AVAILABILITY_PRESENT',
						has_rb_mapping: true
					}
				],
				next_cursor: null
			});
		};

		mod.uiPrefs.last_playlist = null;
		const uninstallStale = mod.installTrackifyFeed();
		await settle();
		assert.equal(fetchCount, 1, 'session 1 must have started its own fetch');

		uninstallStale();

		// A brand-new session installs immediately after. It must not be
		// denied its own initial fetch by the OLD session's still-in-flight
		// `_hydrating` flag (Sol review, PR #3676).
		uninstall = mod.installTrackifyFeed();
		await settle();
		assert.equal(
			fetchCount,
			2,
			'a fresh install right after teardown must start its own fetch, not be denied by the stale flag'
		);

		// The stale session-1 fetch now resolves late; it must not publish.
		gate1.release();
		await settle();
		await settle();
		assert.deepEqual(mod.getTrackifyFeedRows(), [], 'the stale session must not have published');

		// The new session's own fetch resolves and DOES publish.
		gate2.release();
		await settle();
		await settle();
		assert.deepEqual(mod.getTrackifyFeedRows().map((row) => row.stable_id), ['fresh-row']);
	});
});
