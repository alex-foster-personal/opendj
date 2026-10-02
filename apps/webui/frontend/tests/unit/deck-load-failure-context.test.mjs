import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, afterEach, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { engineBlockAfter } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * A failed deck load already measured WHERE it died, and then threw the
 * measurement away.
 *
 * The catch block in audio-engine's load() builds a stage map, stamps
 * `failedAt` on it, and hands it to recordPerfTiming - which is a CLIENT-ONLY
 * ring in the user's browser. The same block then raises an error toast, and
 * that toast is what reaches the server-side webui-client-errors JSONL. So the
 * server learned that deck 2 failed with AUDIO_FILE_MISSING, and learned
 * nothing about whether it died after 40ms in getTrack or after 9 seconds in
 * decodeStems. The one number that says which of those happened never left the
 * machine, and the ring holding it is wiped by the next fader drag.
 *
 * ClientErrorIn.context on the server already accepts
 * dict[str, str|int|float|bool|None] up to 32 keys, so this needs no schema
 * change - only for the stage map to ride along with the report.
 *
 * Regression lines:
 * - if the stage map stops reaching the report then a remote failure is again
 *   "it failed" with no idea which stage was slow or which one threw
 * - if failedAt is dropped when the map is oversized then the single most
 *   diagnostic number is the one that gets clamped away
 * - if the context exceeds 32 keys then the server silently truncates and which
 *   stages survive becomes a coin toss
 * - if a stage named `deck` or `source` can overwrite the reserved keys then a
 *   report cannot be attributed to a deck at all
 * - if a non-finite stage reaches the payload then JSON.stringify writes null
 *   and the row reads as "measured, value unknown" rather than "not measured"
 * - if the context helper throws then error REPORTING replaces the application
 *   error it was supposed to describe
 */

const API_BASE = 'https://deck-load-failure.example.test';

let failureContext;

before(async () => {
	failureContext = await loadTypeScriptModule('src/lib/rb/deck-load-context.ts');
});

//-----------------------------------------------------------------------------
// the context itself
//-----------------------------------------------------------------------------

test('[if] decode or audio fetch fails after getTrack [then] the toast message contains the track title and the reason, [else stop].', () => {
	assert.equal(
		failureContext.formatDeckLoadFailureMessage('Night Ride', 'abc', 'EncodingError: corrupt audio'),
		'Night Ride: EncodingError: corrupt audio'
	);
	assert.equal(
		failureContext.formatDeckLoadFailureMessage(null, 'abc123', 'EncodingError: corrupt audio'),
		'abc123: EncodingError: corrupt audio'
	);
});

test('[if] a track is not on this computer [then] the deck says so in plain words, not the API code, [else stop].', () => {
	// CLOUDSYNC-33: the raw CLOUD_POLICY_UNCONFIGURED text reached a DJ's deck.
	assert.equal(
		failureContext.formatDeckLoadFailureMessage(
			'Outomorrow',
			'abc',
			"AUDIO_NOT_ON_THIS_MACHINE: This file isn't on this computer."
		),
		"Outomorrow: This file isn't on this computer."
	);
	assert.equal(
		failureContext.formatDeckLoadFailureMessage(null, 'abc123', 'AUDIO_FILE_MISSING: iCloud file not downloaded'),
		'abc123: AUDIO_FILE_MISSING: iCloud file not downloaded'
	);
	// Controls: a code with no plain wording, and a message that only looks
	// like one, pass through unchanged.
	assert.equal(
		failureContext.formatDeckLoadFailureMessage('Night Ride', 'abc', 'CLOUD_HYDRATING: fetching'),
		'Night Ride: CLOUD_HYDRATING: fetching'
	);
	assert.equal(
		failureContext.formatDeckLoadFailureMessage('Night Ride', 'abc', 'AUDIO_FILE_MISSING without a colon'),
		'Night Ride: AUDIO_FILE_MISSING without a colon'
	);
});

test('[if] a load fails before its metadata is read [then] the title still comes from that request, [else stop].', async () => {
	// CLOUDSYNC-33 Air check: the toast showed the stable id because the audio
	// fetch rejected before getTrack was read.
	assert.equal(
		await failureContext.settledTrackTitle(Promise.resolve({ track: { title: 'Outomorrow' } })),
		'Outomorrow'
	);
	// Controls: no request, a failed request, and an untitled track give null
	// rather than throwing on the failure path.
	assert.equal(await failureContext.settledTrackTitle(null), null);
	assert.equal(await failureContext.settledTrackTitle(Promise.reject(new Error('404'))), null);
	assert.equal(await failureContext.settledTrackTitle(Promise.resolve({ track: {} })), null);
});

test('[if] a load records its deck-facing message [then] only that same error object reads it back, [else stop].', () => {
	const failed = new Error('AUDIO_NOT_ON_THIS_MACHINE: This file isn\'t on this computer.');
	failureContext.rememberDeckFacingMessage(failed, "Outomorrow: This file isn't on this computer.");
	assert.equal(failureContext.deckFacingMessage(failed), "Outomorrow: This file isn't on this computer.");
	// Controls: another error with the same text, and non-objects, read nothing.
	assert.equal(failureContext.deckFacingMessage(new Error(failed.message)), undefined);
	assert.equal(failureContext.deckFacingMessage('AUDIO_NOT_ON_THIS_MACHINE: x'), undefined);
	assert.equal(failureContext.deckFacingMessage(null), undefined);
});

test('[if] a failed load is worded [then] the title comes from the request when the track was never read, and the banner can read it back, [else stop].', async () => {
	const failed = new Error('AUDIO_NOT_ON_THIS_MACHINE: x');
	const text = await failureContext.failedDeckLoadMessage(
		failed,
		Promise.resolve({ track: { title: 'Outomorrow' } }),
		'224a4561',
		"AUDIO_NOT_ON_THIS_MACHINE: This file isn't on this computer."
	);
	assert.equal(text, "Outomorrow: This file isn't on this computer.");
	assert.equal(failureContext.deckFacingMessage(failed), text);
	// Control: a known title wins and is used as is.
	assert.equal(
		await failureContext.failedDeckLoadMessage(new Error('x'), 'Night Ride', 'abc', 'CLOUD_HYDRATING: fetching'),
		'Night Ride: CLOUD_HYDRATING: fetching'
	);
});

test('the deck banner reads the load path\'s wording, and the load path records it with the title', () => {
	const body = engineBlockAfter('async load(deck: DeckId, stable_id: string, options: DeckLoadOptions = {}): Promise<void> {');
	assert.ok(
		body.includes('await failedDeckLoadMessage(exc, track?.title ?? trackRequest, stable_id,'),
		'if the load stops recording its wording, or stops falling back to its own getTrack ' +
			'request for the title, then the banner shows RbApiError: CODE: detail or the stable id again'
	);
	assert.ok(body.includes('(trackRequest = getTrack(stable_id))'), 'the title request must be the load\'s own getTrack');
	const ipc = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/performance-ipc.svelte.ts', import.meta.url)),
		'utf8'
	).replaceAll('\r\n', '\n');
	const start = ipc.indexOf('function _errorMessage(error: unknown): string {');
	assert.ok(start !== -1, '_errorMessage moved; re-point this guard');
	const fn = ipc.slice(start, ipc.indexOf('\n}\n', start));
	assert.ok(
		fn.includes('deckFacingMessage(error)'),
		'if the banner text ignores the load\'s wording then deck_errors shows the raw API code'
	);
});

test('the context names the source, the deck, and every stage that was measured', () => {
	const context = failureContext.deckLoadFailureContext(2, {
		getTrack: 41,
		fetchAudio: 180,
		fetchWall: 190,
		decodeMix: 820,
		failedAt: 1024,
		audioBytes: 9_400_112
	});

	assert.equal(context.source, 'deck-load');
	assert.equal(context.deck, 2);
	assert.equal(
		context.stage_failedAt,
		1024,
		'failedAt is the number that says WHEN it died; without it the report is ' +
			'back to "it failed"'
	);
	assert.equal(context.stage_decodeMix, 820);
	assert.equal(context.stage_audioBytes, 9_400_112);
	assert.equal(context.stage_getTrack, 41);
});

test('an oversized stage map keeps the diagnostic stages, not an arbitrary 30', () => {
	const stages = { failedAt: 7777 };
	// 60 uninteresting stages, added BEFORE the diagnostic ones so an
	// insertion-order clamp would drop exactly the rows worth keeping.
	for (let i = 0; i < 60; i += 1) stages[`filler${i}`] = i;
	stages.decodeStems = 4100;
	stages.stemProcessorCreate = 260;
	stages.fetchStems = 2200;
	stages.stretchCreate = 90;
	stages.fetchWall = 2600;
	stages.audioBytes = 12_345;

	const context = failureContext.deckLoadFailureContext(1, stages);
	const keys = Object.keys(context);

	assert.ok(
		keys.length <= failureContext.MAX_CONTEXT_KEYS,
		`the server keeps 32 keys and drops the rest, got ${keys.length}`
	);
	for (const stage of [
		'failedAt',
		'fetchWall',
		'decodeMix',
		'fetchStems',
		'decodeStems',
		'stemProcessorCreate',
		'stretchCreate',
		'audioBytes'
	]) {
		if (stages[stage] === undefined) continue;
		assert.equal(
			context[`stage_${stage}`],
			stages[stage],
			`if ${stage} can be clamped away then the clamp is dropping the diagnosis ` +
				'and keeping the filler'
		);
	}
	assert.equal(context.source, 'deck-load');
	assert.equal(context.deck, 1);
});

test('a stage cannot impersonate the reserved keys', () => {
	const context = failureContext.deckLoadFailureContext(3, {
		source: 1,
		deck: 99,
		failedAt: 12
	});
	assert.equal(
		context.source,
		'deck-load',
		'if a stage key could overwrite source then the server dedupe fingerprint ' +
			'changes shape per failure and the row is unattributable'
	);
	assert.equal(context.deck, 3, 'the deck must come from the deck, not from a stage name');
	assert.equal(context.stage_deck, 99, 'the stage is still reported, under its namespaced key');
	assert.equal(context.stage_source, 1);
});

test('a stage that was never measured is absent rather than reported as null', () => {
	const context = failureContext.deckLoadFailureContext(4, {
		failedAt: 30,
		decodeMix: Number.NaN,
		fetchStems: Number.POSITIVE_INFINITY
	});
	assert.equal(context.stage_failedAt, 30);
	assert.ok(
		!('stage_decodeMix' in context),
		'JSON.stringify turns NaN into null, which reads as "measured, value unknown"'
	);
	assert.ok(!('stage_fetchStems' in context));
	assert.deepEqual(JSON.parse(JSON.stringify(context)), context, 'must survive the wire');
});

test('the helper never throws over its input, because it runs inside a catch', () => {
	// Reporting must never replace the original application error, which is the
	// policy client-error-reporting.ts already states for its own writes.
	assert.doesNotThrow(() => failureContext.deckLoadFailureContext(1, {}));
	for (const value of Object.values(failureContext.deckLoadFailureContext(1, {}))) {
		assert.ok(value !== undefined);
	}
});

//-----------------------------------------------------------------------------
// wiring: the context must actually reach the server report
//-----------------------------------------------------------------------------

const MODULE_SOURCE = readFileSync(
	fileURLToPath(new URL('../../src/lib/rb/deck-load-context.ts', import.meta.url)),
	'utf8'
).replaceAll('\r\n', '\n');

test('the deck-load catch block reports through the extracted module, after stamping failedAt', () => {
	const body = engineBlockAfter('async load(deck: DeckId, stable_id: string, options: DeckLoadOptions = {}): Promise<void> {');

	const stampAt = body.indexOf('stages.failedAt = perfMs();');
	const reportAt = body.indexOf(
		'reportDeckLoadFailure(deck, msg, exc, stages, options)'
	);
	assert.ok(
		reportAt !== -1,
		'if the catch stops handing the SAME stages map it just stamped failedAt onto ' +
			'to the reporter then the report describes a different load'
	);
	assert.ok(stampAt !== -1);
	assert.ok(
		stampAt < reportAt,
		'if the report runs before failedAt is stamped then the one number that ' +
			'says when the load died is missing from every report'
	);
	// The connect() failure further down goes through the same reporter (#4036),
	// so a caller that shows its own toast mutes it there too, and it must stamp
	// failedAt first for the same reason the catch above does (#4061 review).
	const connectReportAt = body.indexOf('reportDeckLoadFailure(deck, message, error, stages, options)');
	assert.ok(connectReportAt !== -1, 'the connect() failure must report through the reporter');
	const connectStampAt = body.lastIndexOf('stages.failedAt = perfMs();', connectReportAt);
	assert.ok(
		connectStampAt > reportAt && connectStampAt < connectReportAt,
		'if the connect() failure reports without stamping failedAt then its server ' +
			'report cannot say when the load died'
	);
	assert.ok(
		!body.includes('pushToast(`Deck ${deck} load failed'),
		'the deck-load toast belongs to the reporter module (convention D5: the fat ' +
			'file gets a call site, not a formula), and a raw copy here would toast even ' +
			'when the caller (Trackify) shows its own, which is issue #4036'
	);
	assert.ok(
		!body.includes("recordPerfEvent('deck-load-fail'"),
		'the perf-ring row moved with the toast; leaving one here rings the failure twice'
	);
});

test('the reporter rides the failure context on the toast that already reaches the server', () => {
	assert.ok(
		/const failureContext = deckLoadFailureContext\(deck, stages\);/.test(MODULE_SOURCE),
		'the reported context must be built from the caller stages, not re-measured'
	);
	assert.ok(
		/pushToast\(\s*`Deck \$\{deck\} load failed - \$\{message\}`,\s*'error',\s*undefined,\s*cause,\s*failureContext\s*\)/.test(
			MODULE_SOURCE
		),
		'the failure context must ride the toast report that already reaches the ' +
			'server, or the deck-load row is reported twice with the stages on neither'
	);
	assert.ok(
		/recordPerfEvent\('deck-load-fail', message, deck\)/.test(MODULE_SOURCE),
		'the client perf ring still gets its row; moving the reporting out must not ' +
			'drop the local trace that survives an offline failure'
	);
});

//-----------------------------------------------------------------------------
// wiring: pushToast must forward that context, end to end onto the wire
//-----------------------------------------------------------------------------

function defineGlobal(name, value) {
	Object.defineProperty(globalThis, name, {
		value,
		configurable: true,
		writable: true,
		enumerable: true
	});
}

function makeLocalStorage() {
	const map = new Map();
	return {
		getItem: (key) => (map.has(key) ? map.get(key) : null),
		setItem: (key, value) => {
			map.set(key, String(value));
		},
		removeItem: (key) => {
			map.delete(key);
		}
	};
}

let originalFetch;

function installBrowserGlobals() {
	defineGlobal('localStorage', makeLocalStorage());
	defineGlobal('window', {
		location: { href: 'https://app.example.test/performance' },
		isSecureContext: true,
		addEventListener: () => {}
	});
	defineGlobal('navigator', { userAgent: 'deck-load-failure-test-agent' });
	defineGlobal('crypto', { randomUUID: () => `id-${Math.random()}` });
	defineGlobal('AudioWorkletNode', function AudioWorkletNode() {});
}

after(() => {
	globalThis.fetch = originalFetch;
});

afterEach(() => {
	delete globalThis.window;
});

test('an error toast carries its caller context all the way onto the wire', async () => {
	originalFetch = globalThis.fetch;
	installBrowserGlobals();
	const bodies = [];
	globalThis.fetch = async (input) => {
		bodies.push(await input.clone().json());
		return new Response(JSON.stringify({ event_id: 'e1', stored: true }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};

	const stores = await loadTypeScriptModule('src/lib/stores.svelte.ts', { viteApiBase: API_BASE });
	const context = failureContext.deckLoadFailureContext(2, { failedAt: 1024, decodeMix: 820 });
	stores.pushToast(
		'Deck 2 load failed - AUDIO_FILE_MISSING: no audio',
		'error',
		undefined,
		new Error('AUDIO_FILE_MISSING: no audio'),
		context
	);

	for (let attempt = 0; attempt < 100 && bodies.length === 0; attempt += 1) {
		await new Promise((resolve) => setTimeout(resolve, 5));
	}
	assert.equal(bodies.length, 1, 'exactly one report per failure, not one per reporting site');
	const payload = bodies[0];
	assert.equal(payload.message, 'AUDIO_FILE_MISSING: no audio', 'the real cause, not the toast copy');
	assert.equal(
		payload.context.source,
		'deck-load',
		'if the toast source wins then every deck-load failure lands under the generic ' +
			'"toast" source and cannot be filtered out of the JSONL'
	);
	assert.equal(payload.context.deck, 2);
	assert.equal(
		payload.context.stage_failedAt,
		1024,
		'this is the whole point: the server must learn WHERE the load died'
	);
	assert.equal(payload.context.stage_decodeMix, 820);
	assert.match(
		payload.context.toast_id,
		/^t-[a-z0-9]+-\d+$/,
		'the toast identity must survive too, and as the SAME greppable id the toast ' +
			'printed and copied - a bare counter restarted at 1 every page load'
	);
});

test('a plain error toast still reports, with no context to forward', async () => {
	installBrowserGlobals();
	const bodies = [];
	globalThis.fetch = async (input) => {
		bodies.push(await input.clone().json());
		return new Response(JSON.stringify({ event_id: 'e2', stored: true }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};

	const stores = await loadTypeScriptModule('src/lib/stores.svelte.ts', { viteApiBase: API_BASE });
	stores.pushToast('Deck 1 retired processor cleanup failed - boom', 'error');

	for (let attempt = 0; attempt < 100 && bodies.length === 0; attempt += 1) {
		await new Promise((resolve) => setTimeout(resolve, 5));
	}
	assert.equal(bodies.length, 1, 'the context argument must stay optional');
	assert.equal(
		bodies[0].context.source,
		'toast',
		'if the default source changes then every existing error toast moves in the JSONL'
	);
	assert.match(bodies[0].context.toast_id, /^t-[a-z0-9]+-\d+$/);
});
