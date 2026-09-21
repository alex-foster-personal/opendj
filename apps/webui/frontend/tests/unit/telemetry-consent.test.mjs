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
		consent_required: true,
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
	assert.equal(options.sendClientReports, false, 'no client-report envelopes mid-set');
	assert.equal(options.replaysSessionSampleRate, 1);
	assert.equal(options.replaysOnErrorSampleRate, 1);
	assert.equal(options.environment, 'ship');
	assert.equal(options.release, 'abc123');
	// `integrations` is a function over the SDK's defaults: BrowserSession (the
	// release-health `session` envelopes, measured leaving mid-set through the
	// real loader) is removed, everything else kept, replay appended.
	assert.equal(typeof options.integrations, 'function');
	const integrations = options.integrations([
		{ name: 'BrowserSession' },
		{ name: 'Breadcrumbs' },
		{ name: 'GlobalHandlers' }
	]);
	assert.deepEqual(
		integrations.map((i) => i.name),
		['Breadcrumbs', 'GlobalHandlers', 'Replay']
	);
	const replay = integrations.find((i) => i.name === 'Replay');
	assert.deepEqual(replay.options, {
		maskAllText: true,
		maskAllInputs: true,
		blockAllMedia: true,
		maskAttributes: [...consent.MASKED_ATTRIBUTES],
		beforeAddRecordingEvent: consent.scrubRecordingEvent
	});
	// Library metadata rides in these attributes (TrackTable title={row.title},
	// deck and playlist labels); the SDK default is a subset and not a contract.
	for (const attr of ['title', 'aria-label', 'placeholder', 'alt', 'aria-description']) {
		assert.ok(consent.MASKED_ATTRIBUTES.includes(attr), `${attr} is masked`);
	}
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
		breadcrumbs: [{ message: 'loaded Delilah (PT).wav', data: { title: 'Delilah', route: '/x' } }],
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
	// Breadcrumb data takes the engine's allowlist: `route` survives, `title` is redacted.
	assert.deepEqual(event.breadcrumbs[0].data, { title: '[redacted]', route: '/x' });
	assert.equal(event.request, undefined);
	assert.equal(event.user, undefined);
	// An unknown tag is redacted, never forwarded; ours is stamped on.
	assert.deepEqual(event.tags, { a: '[redacted]', origin: 'browser-sdk' });
	// Control: app routes are diagnostics, not paths, and must survive.
	assert.equal(consent.redactPaths('failed on /performance?deck=1'), 'failed on /performance?deck=1');
});

test('the loader SDK scrub mirrors the engine: tokens, extra/contexts allowlist, frame locals', () => {
	const event = consent.scrubEvent({
		message: 'fetch failed access_token=abcdefghijklmnop for /Users/dev/x.mp3',
		logentry: { message: 'Bearer AAAAAAAAAAAAAAAA rejected', params: ['sk-abcdefghijklmnop'] },
		transaction: '/Users/dev/Music/set.flac',
		exception: {
			values: [
				{
					value: 'boom',
					stacktrace: {
						frames: [
							{ filename: '/Users/dev/app/chunk.js', function: 'load', lineno: 3, vars: { title: 'Delilah' } }
						]
					}
				}
			]
		},
		extra: { title: 'Delilah', deck_id: '1', nested: { artist: 'Fred' } },
		contexts: {
			browser: { name: 'Chrome', version: '141 /Users/dev/x.mp3' },
			playback: { title: 'Delilah', deck_id: '2' }
		},
		server_name: 'silver.local',
		tags: { error_id: 'eid-1', album: 'Actual Life' }
	});
	assert.equal(event.message, 'fetch failed [filtered] for <path.mp3>');
	assert.equal(event.logentry.message, '[filtered] rejected');
	assert.deepEqual(event.logentry.params, ['[filtered]']);
	assert.equal(event.transaction, '<path.flac>');
	const frame = event.exception.values[0].stacktrace.frames[0];
	assert.equal(frame.vars, undefined, 'frame locals never leave');
	assert.equal(frame.filename, '<path.js>');
	assert.equal(frame.function, 'load');
	assert.deepEqual(event.extra, { title: '[redacted]', deck_id: '1', nested: '[redacted]' });
	// SDK-built block keeps its shape with string leaves scrubbed; an app block is allowlisted.
	assert.deepEqual(event.contexts.browser, { name: 'Chrome', version: '141 <path.mp3>' });
	assert.deepEqual(event.contexts.playback, { title: '[redacted]', deck_id: '2' });
	assert.equal(event.server_name, undefined);
	assert.deepEqual(event.tags, { error_id: 'eid-1', album: '[redacted]', origin: 'browser-sdk' });
	// Fail closed: an event the scrubber cannot walk is dropped, not sent as is.
	const poisoned = { get message() { throw new Error('unreadable'); } };
	assert.equal(consent.scrubEvent(poisoned), null);
});

test('the allowlists match the engine verbatim', async () => {
	const { readFileSync } = await import('node:fs');
	const py = readFileSync(new URL('../../../../../apps/shared/telemetry/scrub.py', import.meta.url), 'utf8');
	const block = (name) => {
		const m = py.match(new RegExp(`${name}[^=]*=\\s*frozenset\\(\\s*\\{([\\s\\S]*?)\\}\\s*\\)`));
		assert.ok(m, `${name} not found in scrub.py`);
		return new Set([...m[1].matchAll(/"([^"]+)"/g)].map((x) => x[1]));
	};
	assert.deepEqual(new Set(consent.ALLOWED_CONTEXT_KEYS), block('ALLOWED_CONTEXT_KEYS'));
	assert.deepEqual(new Set(consent.SDK_CONTEXT_BLOCKS), block('SDK_CONTEXT_BLOCKS'));
});

test('an operator-explicit host is never asked: a decline there could not close the gate', async () => {
	const scheduler = manualScheduler();
	stopBoot = consent.bootTelemetryConsent({
		scheduler,
		isLive: () => false,
		showDialog: async () => assert.fail('no dialog on an explicit enable'),
		fetchConsent: async () => consentBody({ consent_required: false }),
		loadScript: () => assert.fail('undecided: nothing to load')
	});
	await scheduler.release();
	assert.equal(consent.isConsentDialogOpen(), false);
	assert.equal(consent.shouldAsk(consentBody({ consent_required: false })), false);
	assert.equal(consent.shouldAsk(consentBody()), true, 'control: the default-on build asks');
});

test('a stored acceptance loads nothing when this boot has telemetry off', async () => {
	const scheduler = manualScheduler();
	stopBoot = consent.bootTelemetryConsent({
		scheduler,
		isLive: () => false,
		fetchConsent: async () =>
			consentBody({ decision: 'accepted', terms_version: '2026-09-21', telemetry_active: false }),
		loadScript: () => assert.fail('opted-out boot must not load the loader, whatever the file says')
	});
	await scheduler.release();
	assert.equal(consent.isConsentDialogOpen(), false);
	assert.equal(globalThis.window.sentryOnLoad, undefined);
});

test('a replay click breadcrumb never carries the attribute values the SDK writes into its selector', () => {
	// Measured through the real loader (Mon 21 Sep 2026): rrweb masked the DOM
	// snapshot, but the ui.click breadcrumb's selector still named the track.
	const click = {
		type: 5,
		timestamp: 1,
		data: {
			tag: 'breadcrumb',
			payload: {
				category: 'ui.click',
				message:
					'td.c-title.s-X[title="webkit-fixture-b-124bpm"] > button.deck-target[title="Load onto deck 1"][type="button"][data-testid="load-1"]',
				data: {
					nodeId: 7,
					node: {
						id: 7,
						tagName: 'button',
						textContent: '*',
						attributes: {
							class: 'deck-target',
							title: 'Load onto deck 1',
							'aria-label': 'webkit-fixture-b-124bpm',
							alt: 'cover of webkit-fixture-b-124bpm',
							testId: 'load-1',
							role: 'button'
						}
					}
				}
			}
		}
	};
	const out = consent.scrubRecordingEvent(click);
	assert.equal(out.data.payload.message, 'td.c-title.s-X[title="[filtered]"]');
	assert.deepEqual(out.data.payload.data.node.attributes, {
		class: 'deck-target',
		title: '[filtered]',
		'aria-label': '[filtered]',
		alt: '[filtered]',
		testId: 'load-1',
		role: 'button'
	});
	assert.ok(!JSON.stringify(out).includes('webkit-fixture-b-124bpm'), 'no track text survives');
	// Control: a performance span and a non-custom rrweb event pass untouched.
	const span = {
		type: 5,
		data: { tag: 'performanceSpan', payload: { op: 'resource.fetch', description: '/api/v1/x' } }
	};
	assert.equal(consent.scrubRecordingEvent(span), span);
	const mutation = { type: 3, data: { source: 0 } };
	assert.equal(consent.scrubRecordingEvent(mutation), mutation);
	// Fail closed: a payload the scrub cannot walk is dropped, never buffered.
	const poisoned = { type: 5, data: { tag: 'breadcrumb', payload: { data: { node: null } } } };
	Object.defineProperty(poisoned.data.payload, 'message', {
		get() {
			throw new Error('boom');
		}
	});
	assert.equal(consent.scrubRecordingEvent(poisoned), null);
});

test('the error-event click breadcrumb gets the same selector scrub', () => {
	const crumb = consent.scrubBreadcrumb({
		category: 'ui.click',
		message: 'div.deck[aria-label="webkit-fixture-b-124bpm"] > button[type="button"]'
	});
	assert.equal(crumb.message, 'div.deck[aria-label="[filtered]"]');
	// Only the SDK's DOM breadcrumbs are selector paths; a console line with a
	// ` > ` in it is scrubbed as text, not thrown away as an unparseable path.
	const line = consent.scrubBreadcrumb({
		category: 'console',
		message: 'decode a > b /Users/dev/Music/x.mp3'
	});
	assert.equal(line.message, 'decode a > b <path.mp3>');
});

test('a selector the SDK wrote with an unescaped title keeps only the prefix before it', () => {
	// The SDK writes `[title="${value}"]` verbatim, so the text after the
	// first library-content attribute may be the value itself, however it
	// parses: `A"] > span#PRIVATE[title="B` yields a clean path with #PRIVATE
	// in it (Codex on #3752). Keep the DOM-derived prefix, drop the rest.
	const ui = (message) => consent.scrubBreadcrumb({ category: 'ui.click', message }).message;
	assert.equal(
		ui('td.c-title[title="A"] > span#PRIVATE[title="B"] > span.title-text'),
		'td.c-title[title="[filtered]"]'
	);
	assert.equal(ui('td.c-title[title="Song "Live" Mix"] > span.title-text'), 'td.c-title[title="[filtered]"]');
	assert.equal(ui('td.c-title[title="A > B"] > span.title-text'), 'td.c-title[title="[filtered]"]');
	assert.equal(ui('td.c-title[title="Mix [radio] v1.2"]'), 'td.c-title[title="[filtered]"]');
	// Kept, app-authored attributes before the first library one survive; the
	// path joiner and the ancestors do too.
	assert.equal(
		ui('main#app.s-X.rb-row-first > button.deck-target[type="button"][data-testid="load-1"][title="x"] > span'),
		'main#app.s-X.rb-row-first > button.deck-target[type="button"][data-testid="load-1"][title="[filtered]"]'
	);
	// No library attribute at all: the path passes whole when it parses.
	assert.equal(ui('main#app > button[type="button"]'), 'main#app > button[type="button"]');
	// Not a selector, a fragment, or a prefix that does not parse: filtered whole.
	assert.equal(ui('not a selector at all'), '[filtered]');
	assert.equal(ui('B"] > span'), '[filtered]');
	assert.equal(ui('[title="x"]'), '[filtered]');
	assert.equal(ui('td.c-title > [title="x"]'), '[filtered]');
	assert.equal(ui('td.c-title[type="a"b"][title="x"]'), '[filtered]');
	// The same through the recording hook.
	const rec = consent.scrubRecordingEvent({
		type: 5,
		data: {
			tag: 'breadcrumb',
			payload: { category: 'ui.input', message: 'input.search[name="Song "Live""]' }
		}
	});
	assert.equal(rec.data.payload.message, 'input.search[name="[filtered]"]');
});
