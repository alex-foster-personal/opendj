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
});
