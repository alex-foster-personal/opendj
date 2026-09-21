/**
 * OBS-05 / OBS-06: the consent dialog and the replay loader.
 *
 * - if an undecided tester is not asked while there is something to consent
 *   to, the engine holds every send forever and nobody learns why.
 * - if replay starts before acceptance, the terms are decoration.
 * - if the loader is initialized without masking, track names ship in replays.
 * - if the live gate does not stop the replay while a deck plays, a mix is
 *   recorded and flushed mid-set.
 */
import assert from 'node:assert/strict';
import { after, afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://consent.example.test';
const LOADER = 'https://js-de.sentry-cdn.com/00ff00ff00ff00ff.min.js';

let consent;
let originalFetch;
/** The teardown of the boot under test; afterEach runs it (it is the reset). */
let stopBoot = null;

function defineGlobal(name, value) {
	Object.defineProperty(globalThis, name, {
		value,
		configurable: true,
		writable: true,
		enumerable: true
	});
}

function installBrowserGlobals() {
	defineGlobal('window', {});
	defineGlobal('document', {
		querySelector: () => null,
		createElement: () => ({}),
		head: { appendChild: () => {} }
	});
}

function consentBody(overrides = {}) {
	return {
		decision: 'undecided',
		terms_version: null,
		terms_current_version: '2026-09-21',
		decided_at: null,
		telemetry_active: true,
		environment: 'ship',
		release: 'abc123',
		replay_loader_url: LOADER,
		replay_session_sample_rate: 1,
		replay_on_error_sample_rate: 1,
		...overrides
	};
}

/** A scheduler whose deferred work runs when the test says so. */
function manualScheduler() {
	const deferred = [];
	return {
		defer: (label, fn) => deferred.push({ label, fn }),
		start: () => () => {},
		async release() {
			for (const item of deferred.splice(0)) await item.fn();
		},
		labels: () => deferred.map((d) => d.label)
	};
}

/** A fake loader SDK: records init options and exposes a controllable replay. */
function fakeSentry() {
	const calls = { init: [], tags: [], starts: 0, stops: 0, stopOptions: [] };
	const replay = {
		start: () => {
			calls.starts += 1;
		},
		stop: (options) => {
			calls.stops += 1;
			calls.stopOptions.push(options ?? null);
		}
	};
	return {
		calls,
		sdk: {
			init: (options) => calls.init.push(options),
			setTag: (key, value) => calls.tags.push([key, value]),
			getReplay: () => replay,
			replayIntegration: (options) => ({ name: 'Replay', options })
		}
	};
}

before(async () => {
	originalFetch = globalThis.fetch;
	installBrowserGlobals();
	consent = await loadTypeScriptModule('tests/unit/fixtures/telemetry-consent-entry.ts', {
		viteApiBase: API_BASE
	});
});

afterEach(() => {
	stopBoot?.();
	stopBoot = null;
	installBrowserGlobals();
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('an undecided tester with something to consent to is asked, after the boot window', async () => {
	const scheduler = manualScheduler();
	stopBoot = consent.bootTelemetryConsent({
		scheduler,
		isLive: () => false,
		showDialog: async () => () => {},
		fetchConsent: async () => consentBody(),
		loadScript: () => assert.fail('replay must not load before acceptance')
	});
	assert.equal(consent.isConsentDialogOpen(), false, 'nothing before the window closes');
	assert.deepEqual(scheduler.labels(), ['telemetry-consent:fetch']);
	await scheduler.release();
	assert.equal(consent.isConsentDialogOpen(), true);
	assert.equal(consent.currentConsent().terms_current_version, '2026-09-21');
});

test('nothing to consent to means no dialog', async () => {
	const scheduler = manualScheduler();
	stopBoot = consent.bootTelemetryConsent({
		scheduler,
		isLive: () => false,
		fetchConsent: async () =>
			consentBody({ telemetry_active: false, replay_loader_url: null }),
		loadScript: () => assert.fail('no loader url, nothing to load')
	});
	await scheduler.release();
	assert.equal(consent.isConsentDialogOpen(), false);
});

test('a stored decision is never asked again; declined loads nothing', async () => {
	const scheduler = manualScheduler();
	stopBoot = consent.bootTelemetryConsent({
		scheduler,
		isLive: () => false,
		fetchConsent: async () => consentBody({ decision: 'declined', terms_version: '2026-09-21' }),
		loadScript: () => assert.fail('declined must not load the loader')
	});
	await scheduler.release();
	assert.equal(consent.isConsentDialogOpen(), false);
});

test('accepting records the current terms version and then loads the loader, masked', async () => {
	const scheduler = manualScheduler();
	const loaded = [];
	const ticks = [];
	let live = false;
	stopBoot = consent.bootTelemetryConsent({
		scheduler,
		isLive: () => live,
		showDialog: async () => () => {},
		fetchConsent: async () => consentBody(),
		loadScript: (url) => loaded.push(url),
		every: (_ms, fn) => {
			ticks.push(fn);
			return () => {};
		}
	});
	await scheduler.release();
	assert.equal(consent.isConsentDialogOpen(), true);

	let put;
	const answered = await consent.answerConsent('accepted', async (body) => {
		put = body;
		return consentBody({ decision: 'accepted', terms_version: body.terms_version });
	});
	assert.deepEqual(put, { decision: 'accepted', terms_version: '2026-09-21' });
	assert.equal(answered.decision, 'accepted');
	assert.equal(consent.isConsentDialogOpen(), false);
	assert.deepEqual(loaded, [LOADER], 'the loader is fetched exactly once, after acceptance');

	// The loader calls window.sentryOnLoad before its own init; that is where
	// the effective options come from.
	const fake = fakeSentry();
	globalThis.window.Sentry = fake.sdk;
	assert.equal(typeof globalThis.window.sentryOnLoad, 'function');
	globalThis.window.sentryOnLoad();
	assert.equal(fake.calls.init.length, 1);
	const options = fake.calls.init[0];
	assert.equal(options.sendDefaultPii, false);
	assert.equal(options.replaysSessionSampleRate, 1);
	assert.equal(options.replaysOnErrorSampleRate, 1);
	assert.equal(options.environment, 'ship');
	assert.equal(options.release, 'abc123');
	const replay = options.integrations.find((i) => i.name === 'Replay');
	assert.deepEqual(replay.options, { maskAllText: true, maskAllInputs: true, blockAllMedia: true });
	assert.deepEqual(fake.calls.tags, [['origin', 'browser-sdk']]);
	assert.equal(ticks.length, 1, 'the live gate poll is armed');
	// The loader SDK's own error capture: scrubbed when idle, DROPPED while a
	// deck is live (the engine's any_deck_live gate cannot see these events).
	const idle = options.beforeSend({ message: 'decode failed /Users/dev/Music/x.mp3', tags: {} });
	assert.equal(idle.message, 'decode failed <path.mp3>');
	assert.equal(idle.tags.origin, 'browser-sdk');
	live = true;
	assert.equal(options.beforeSend({ message: 'mid-set' }), null, 'nothing leaves while live');
	live = false;
});

test('the live watcher stops the replay at once and discards the tail, ahead of any poll', async () => {
	const scheduler = manualScheduler();
	let live = false;
	let notify = null;
	let tick;
	stopBoot = consent.bootTelemetryConsent({
		scheduler,
		isLive: () => live,
		fetchConsent: async () => consentBody({ decision: 'accepted', terms_version: '2026-09-21' }),
		loadScript: () => {},
		every: (_ms, fn) => {
			tick = fn;
			return () => {};
		},
		watchLive: (onChange) => {
			notify = onChange;
			return () => {
				notify = null;
			};
		}
	});
	await scheduler.release();
	const fake = fakeSentry();
	globalThis.window.Sentry = fake.sdk;
	globalThis.window.sentryOnLoad();
	assert.equal(typeof notify, 'function', 'the watcher is armed at init');

	live = true;
	notify(true);
	assert.equal(fake.calls.stops, 1, 'stopped in the watcher callback, no poll needed');
	assert.deepEqual(fake.calls.stopOptions, [{ forceFlush: false }], 'the buffered tail is discarded, not flushed');
	tick();
	assert.equal(fake.calls.stops, 1, 'the poll does not stop it a second time');

	live = false;
	notify(false);
	tick();
	tick();
	assert.equal(fake.calls.starts, 1, 'restarts after two idle ticks');
	stopBoot();
	stopBoot = null;
	assert.equal(notify, null, 'teardown unsubscribes the watcher');
});

test('the live gate stops the replay while a deck plays and restarts after two idle ticks', async () => {
	const scheduler = manualScheduler();
	let live = false;
	let tick;
	stopBoot = consent.bootTelemetryConsent({
		scheduler,
		isLive: () => live,
		fetchConsent: async () => consentBody({ decision: 'accepted', terms_version: '2026-09-21' }),
		loadScript: () => {},
		every: (_ms, fn) => {
			tick = fn;
			return () => {};
		}
	});
	await scheduler.release();
	const fake = fakeSentry();
	globalThis.window.Sentry = fake.sdk;
	globalThis.window.sentryOnLoad();

	tick();
	assert.equal(fake.calls.stops, 0, 'idle: recording continues untouched');
	live = true;
	tick();
	tick();
	assert.equal(fake.calls.stops, 1, 'live: stopped once, not once per tick');
	assert.deepEqual(fake.calls.stopOptions, [{ forceFlush: false }], 'the poll path discards too');
	assert.equal(fake.calls.starts, 0);
	live = false;
	tick();
	assert.equal(fake.calls.starts, 0, 'one idle tick is a stop still ringing out');
	tick();
	assert.equal(fake.calls.starts, 1, 'two idle ticks: recording again');
	live = true;
	tick();
	assert.equal(fake.calls.stops, 2, 'and the gate keeps working on the next set');
});

test('the loader SDK scrub reduces paths and audio filenames to their extension', () => {
	const event = consent.scrubEvent({
		message: 'could not decode /Users/maintainer/Music/Fred again - Delilah.mp3',
		exception: { values: [{ value: 'read C:\\Music\\Artist - Title.flac failed' }] },
		breadcrumbs: [{ message: 'loaded Delilah (PT).wav', data: { title: 'Delilah' } }],
		request: { url: 'x' },
		user: { id: 'u' },
		tags: { a: 'b' }
	});
	assert.equal(event.message, 'could not decode <path.mp3>');
	assert.equal(event.exception.values[0].value, 'read <path.flac> failed');
	// The bare-filename rule stops at whitespace, exactly as the engine's does
	// (`scrub.py` `_PATH_RE`, third branch); a rooted path is what carries the
	// artist, and that branch allows spaces. Same output on both sides by design.
	assert.equal(event.breadcrumbs[0].message, 'loaded Delilah <path.wav>');
	assert.equal(event.breadcrumbs[0].data, undefined);
	assert.equal(event.request, undefined);
	assert.equal(event.user, undefined);
	assert.deepEqual(event.tags, { a: 'b', origin: 'browser-sdk' });
	// Control: app routes are diagnostics, not paths, and must survive.
	assert.equal(consent.redactPaths('failed on /performance?deck=1'), 'failed on /performance?deck=1');
});
