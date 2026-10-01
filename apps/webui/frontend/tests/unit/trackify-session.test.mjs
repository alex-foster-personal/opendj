/**
 * Tests for `trackify-session.svelte.ts`'s teardown, run against the REAL
 * performance dispatcher (only the engine's audio transport is faked),
 * matching trackify-autoplay-controller.test.mjs's convention.
 *
 * Sol review, PR #3676: Svelte does not await an async `onMount` cleanup, so
 * navigating from Trackify straight to Gig can mount and claim the shared
 * engine WHILE Trackify's teardown is still suspended on its stop-playback
 * dispatch. When that dispatch resolves, an unconditional `engine.dispose()`
 * would tear down the newly-mounted Gig session's engine instead of
 * Trackify's own. This is the reverse of the Gig-to-Trackify race PERFMODE-14
 * / PERFMODE-15 already guard with `noteGigRuntimeMounted`/
 * `releaseGigRuntime`'s generation check -- this file proves the same
 * protection now also covers Trackify's own teardown.
 */
import assert from 'node:assert/strict';
import { afterEach, before, beforeEach, describe, it } from 'node:test';

import { importBundledSource } from './import-bundled-source.mjs';
import { bundleTypeScriptModule } from './load-typescript.mjs';

/** @type {string} */
let bundleText;
/** @type {Record<string, any>} */
let entry;

/** Fakes the audio transport boundary exactly like
 * trackify-autoplay-controller.test.mjs's installFakeTransport, plus a
 * spyable `dispose` (the real one needs a browser AudioContext). */
function installFakeEngine() {
	const disposeCalls = [];
	entry.engine.load = async () => {};
	entry.engine.unload = async () => {};
	entry.engine.play = async (deckId) => {
		entry.deckStates[deckId].playing = true;
	};
	entry.engine.pause = async (deckId) => {
		entry.deckStates[deckId].playing = false;
	};
	entry.engine.dispose = async () => {
		disposeCalls.push('dispose');
	};
	return { disposeCalls };
}

describe('trackify session teardown vs a concurrent Gig mount (PERFMODE-15)', { concurrency: false }, () => {
	before(async () => {
		bundleText = await bundleTypeScriptModule('tests/unit/fixtures/trackify-session-entry.ts', {
			dev: true
		});
		// $state is an identity function under test (loadTypeScriptModule sets
		// this normally; this file bundles directly so the bundle text can be
		// cached once in `before`, matching trackify-autoplay-controller's
		// convention).
		globalThis.__musicDjToolsTestState = (value) => value;
		globalThis.__musicDjToolsTestState.snapshot = (value) =>
			value === undefined ? undefined : JSON.parse(JSON.stringify(value));
	});

	beforeEach(async () => {
		entry = await importBundledSource(bundleText, 'trackify-session-entry');
		globalThis.window = {};
		globalThis.fetch = async () =>
			new Response(JSON.stringify({ items: [], next_cursor: null }), {
				status: 200,
				headers: { 'content-type': 'application/json' }
			});
		// installTrackifySession() installs the performance IPC layer itself
		// (unlike installTrackifyAutoplay/installTrackifyFeed, which don't),
		// so this file must not also install it -- unlike
		// trackify-autoplay-controller.test.mjs, which drives those two
		// installers directly rather than through installTrackifySession.
		entry.e2ePrimeTrackifyFeed([]);
		entry.resetLibraryModeRuntimeForTest();
	});

	afterEach(() => {
		delete globalThis.window;
	});

	it('does not dispose the engine once a newer session has claimed it since this one mounted', async () => {
		const { disposeCalls } = installFakeEngine();

		const uninstall = entry.installTrackifySession();

		// Simulate Gig mounting after Trackify, before Trackify's teardown
		// runs -- e.g. the operator navigated Trackify -> Gig, and Gig's
		// onMount (the real `noteGigRuntimeMounted` call site) claimed the
		// shared engine while Trackify's own async cleanup (unawaited by
		// SvelteKit's route transition) was still suspended on its
		// stop-playback dispatch. This is the reverse of the Gig-to-Trackify
		// race PERFMODE-14/15 already guard: whichever mount is MOST RECENT
		// by the time a stale teardown reaches its dispose check must win.
		entry.noteGigRuntimeMounted();

		await uninstall();

		assert.deepEqual(
			disposeCalls,
			[],
			'a teardown superseded by a newer mount must not dispose that session\'s engine'
		);
	});

	it('holds the ANLZ cache to one entry while mounted and releases it on teardown', async () => {
		installFakeEngine();
		const tierCap = entry.anlzEntryCap();
		assert.ok(tierCap > entry.TRACKIFY_ANLZ_ENTRY_CAP, 'control: the tier allows more than Trackify keeps');

		const uninstall = entry.installTrackifySession();
		assert.equal(entry.effectiveAnlzEntryCap(), entry.TRACKIFY_ANLZ_ENTRY_CAP);

		await uninstall();
		// Overshoot control: Gig and Library get their tier cap back.
		assert.equal(entry.effectiveAnlzEntryCap(), tierCap);
	});

	it('control: disposes the engine normally when nothing else mounted during teardown', async () => {
		const { disposeCalls } = installFakeEngine();

		const uninstall = entry.installTrackifySession();
		await uninstall();

		// Control for the overshoot direction: a fix that never disposes
		// (e.g. an inverted or permanently-tripped generation check) would
		// pass the test above and fail this one.
		assert.deepEqual(disposeCalls, ['dispose']);
	});
});
