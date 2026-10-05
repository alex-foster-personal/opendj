/**
 * The browser emitter that feeds the Open DJ own-deck source.
 *
 * PR #605 shipped the server side with 28 passing tests and no caller, so
 * REC recorded zero tracks from our own decks. These are the regression
 * lines for the half that closes the loop:
 *
 * - if a snapshot stops carrying an explicit UTC offset then the server
 *   refuses it and the set records nothing
 * - if playing/audible stop being real booleans then the server 422s the
 *   whole batch
 * - if audible-with-no-track stops being refused locally then one impossible
 *   deck loses up to 600 good observations with it
 * - if the emitter posts while no recorder is running then it spams an
 *   endpoint that can only answer 409
 * - if a 409 stops returning the emitter to idle then it retries forever
 * - if a transport failure stops keeping its batch buffered then a blip
 *   silently deletes the seconds it had already seen
 * - if a batch is ever posted larger than 600, or out of observed_at order,
 *   then the server rejects it outright
 * - if /performance stops installing the emitter then the whole loop is open
 *   again and REC records zero Open DJ tracks, which no test of this module
 *   in isolation can see (the same failure app-init.test.mjs was written for)
 * - if a deck behind a closed fader or a parked crossfader is reported audible
 *   then pre-cueing writes track_loaded rows for tracks nobody heard
 * - if buffered snapshots survive a recorder swap then one set's playback is
 *   filed under another set
 * - if a deck unmapped by a partial `?extroute=` is scored through the internal
 *   master then headphone-only playback writes a set row
 * - if observing itself waits for the boot window then a set already running
 *   loses ~15s and a short cue entirely; and if teardown stops clearing the
 *   interval then it outlives the route
 * - if a recorder check that FAILED is read as confirmation then the backlog
 *   goes out on the strength of a question nobody answered
 * - if two ticks overlap then the second flushes past the identity check the
 *   first is still waiting on
 * - if a stale flush's 409 clears the whole buffer then it deletes the fresh
 *   samples of the recording that is running right now
 * - if one batch may mix two sessions then it carries one session id and the
 *   server files half of it under the wrong set
 * - if the re-verification drop is scoped to the whole buffer then it deletes
 *   the run belonging to the recorder that is live
 * - if a stale 409 resets phase and session unconditionally then it forgets a
 *   recorder the emitter had already found, and stops sampling it
 * - if the clock floor outlives the session it belonged to then a wall clock
 *   that regressed between two recordings makes the client refuse a whole set
 *   the server would have accepted
 *
 * WHAT DRIVES THESE (Codex #650 P1, partly upheld). The engine half no longer
 * uses a hand-built state object. `fixtures/deck-observer-entry.ts` bundles the
 * real `deckStates`, the real `mixerState`, the real `queryPerformanceState`
 * and the real projection into ONE module graph, so these tests drive the
 * production records through the production query path.
 *
 * The HTTP half still replaces `globalThis.fetch`. Node has no AudioContext, so
 * a unit test cannot make a real deck sound; the real transport, the real
 * server, the real recorder and the real sqlite row are covered by the
 * end-to-end run recorded on PR #650 instead. Intercepting `fetch` here is also
 * the established convention of this suite (`api-client`, `app-init`,
 * `usage-heartbeat` and others), so changing it in one file would buy nothing
 * and diverge from the rest.
 */
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync } from 'node:fs';
import { engineBlockAfter } from './engine-source.mjs';
import { fileURLToPath } from 'node:url';
import { after, afterEach, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://engine.example.test';

let emitterModule;
/** The real engine records + real query path + real projection, one graph. */
let live;
let originalFetch;
let originalConsoleError;
let calls;
let respond;

/**
 * Put deck 1 on the REAL engine records as a loaded, presented, heard deck and
 * return the REAL `queryPerformanceState()` snapshot.
 *
 * These are the engine's own `deckStates` / `mixerState` objects, not a copy,
 * so the projection under test reads exactly what production reads. `audible`
 * is written directly because it is normally set by the output presentation
 * clock, and node has no AudioContext to run one.
 */
function liveState({ deck = {}, channel = {}, mixer = {}, stems = null } = {}) {
	const st = live.deckStates[1];
	st.stable_id = 'sid-1';
	st.title = 'Open Windows';
	st.artist = 'Cass Delaney';
	st.duration_ms = 220000;
	st.position_ms = 12000;
	st.playing = true;
	st.audible = true;
	if (stems !== null) st.stems = stems;
	Object.assign(st, deck);
	for (const id of [2, 3, 4]) {
		const idle = live.deckStates[id];
		idle.stable_id = null;
		idle.title = null;
		idle.artist = null;
		idle.duration_ms = null;
		idle.position_ms = 0;
		idle.playing = false;
		idle.audible = false;
	}
	live.mixerState.master = 1;
	live.mixerState.crossfader = 0.5;
	Object.assign(live.mixerState, mixer);
	const ch = live.mixerState.channels[1];
	ch.trim = 0.5;
	ch.fader = 1;
	ch.assign = 'A';
	Object.assign(ch, channel);
	return live.queryPerformanceState();
}

function jsonResponse(status, body) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

before(async () => {
	originalFetch = globalThis.fetch;
	originalConsoleError = console.error;
	emitterModule = await loadTypeScriptModule('src/lib/sets/deck-observer-emitter.ts', {
		viteApiBase: API_BASE
	});
	live = await loadTypeScriptModule('tests/unit/fixtures/deck-observer-entry.ts', {
		viteApiBase: API_BASE
	});
});

beforeEach(() => {
	calls = [];
	respond = () => jsonResponse(202, { accepted: 1, status: {} });
	// openapi-fetch hands `fetch` a Request, not (url, init).
	globalThis.fetch = async (request) => {
		const raw = await request.clone().text();
		const call = {
			url: request.url,
			method: request.method,
			body: raw === '' ? null : JSON.parse(raw)
		};
		calls.push(call);
		return respond(call);
	};
	console.error = () => {};
});

afterEach(() => {
	console.error = originalConsoleError;
});

after(() => {
	globalThis.fetch = originalFetch;
	console.error = originalConsoleError;
});

// ------------------------------------------------------------- projection ---

test('a snapshot carries an explicit UTC offset and real booleans', () => {
	const { toWireSnapshot } = live;
	const wire = live.toWireSnapshot(liveState(), new Date(Date.UTC(2026, 8, 1, 12, 0, 0)));

	assert.equal(wire.observed_at, '2026-09-01T12:00:00.000Z');
	assert.match(wire.observed_at, /(Z|[+-]\d{2}:\d{2})$/);
	assert.deepEqual(Object.keys(wire.decks).sort(), ['1', '2', '3', '4']);
	assert.equal(wire.decks['1'].playing, true);
	assert.equal(wire.decks['1'].audible, true);
	assert.equal(wire.decks['1'].stable_id, 'sid-1');
	assert.equal(wire.decks['1'].duration_ms, 220000);
	assert.equal(wire.decks['2'].stable_id, null);
	assert.equal(wire.decks['2'].audible, false);
});

test('audible with no track loaded is refused before it reaches the wire', () => {
	const { toWireSnapshot, DeckProjectionError } = live;
	assert.throws(
		() => live.toWireSnapshot(liveState({ deck: { stable_id: null, audible: true } }), new Date()),
		(error) => error instanceof DeckProjectionError && /audible with no track/.test(error.message)
	);
});

test('a non-boolean transport flag is refused rather than coerced', () => {
	const { toWireSnapshot, DeckProjectionError } = live;
	assert.throws(
		() => live.toWireSnapshot(liveState({ deck: { playing: 'false' } }), new Date()),
		(error) => error instanceof DeckProjectionError && /playing must be a bool/.test(error.message)
	);
});

test('a negative or non-finite position is refused', () => {
	const { toWireSnapshot, DeckProjectionError } = live;
	assert.throws(
		() => live.toWireSnapshot(liveState({ deck: { position_ms: -1 } }), new Date()),
		DeckProjectionError
	);
	assert.throws(
		() => live.toWireSnapshot(liveState({ deck: { position_ms: Number.NaN } }), new Date()),
		DeckProjectionError
	);
});

test('a zero-length deck travels as null duration, which the server accepts', () => {
	const { toWireSnapshot } = live;
	const wire = live.toWireSnapshot(liveState({ deck: { duration_ms: 0 } }), new Date());
	assert.equal(wire.decks['1'].duration_ms, null);
});

// ---------------------------------------------------------------- emitter ---

test('nothing is posted while no recording is running', async () => {
	const emitter = emitterModule.createDeckObserverEmitter({
		readState: () => liveState()
	});
	respond = () => jsonResponse(200, { active: false, session_id: null, pid: null, owned: false, recoverable: false });

	await emitter.tick();
	await emitter.tick();

	assert.deepEqual(
		calls.map((call) => `${call.method} ${call.url}`),
		[`GET ${API_BASE}/api/sets/recorder`]
	);
	assert.equal(emitter.status().phase, 'idle');
	assert.equal(emitter.status().sampled, 0);
});

test('a live recorder makes each tick post the deck snapshots it sampled', async () => {
	let clock = Date.UTC(2026, 8, 1, 12, 0, 0);
	const emitter = emitterModule.createDeckObserverEmitter({
		readState: () => liveState(),
		now: () => new Date(clock)
	});
	respond = (call) =>
		call.url.endsWith('/api/sets/recorder')
			? jsonResponse(200, {
					active: true,
					session_id: '2026-09-01T12-00-00',
					pid: 42,
					owned: true,
					recoverable: false
				})
			: jsonResponse(202, { accepted: 1, status: {} });

	await emitter.tick();
	clock += 1000;
	await emitter.tick();

	const posts = calls.filter((call) => call.method === 'POST');
	assert.equal(posts.length, 2);
	assert.equal(posts[0].url, `${API_BASE}/api/sets/deck-observations`);
	assert.equal(posts[0].body.snapshots.length, 1);
	assert.equal(posts[0].body.snapshots[0].decks['1'].stable_id, 'sid-1');
	assert.equal(posts[1].body.snapshots[0].observed_at, '2026-09-01T12:00:01.000Z');

	const status = emitter.status();
	assert.equal(status.phase, 'emitting');
	assert.equal(status.session_id, '2026-09-01T12-00-00');
	assert.equal(status.posted, 2);
	assert.equal(status.buffered, 0);
});

test('a 409 returns the emitter to idle instead of retrying forever', async () => {
	const emitter = emitterModule.createDeckObserverEmitter({
		readState: () => liveState(),
		recorderPollMs: 60_000
	});
	respond = (call) =>
		call.url.endsWith('/api/sets/recorder')
			? jsonResponse(200, { active: true, session_id: 's', pid: 1, owned: true, recoverable: false })
			: jsonResponse(409, { detail: 'no HTTP-owned recorder is active' });

	await emitter.tick();
	assert.equal(emitter.status().phase, 'idle');
	assert.equal(emitter.status().buffered, 0);
	assert.equal(emitter.status().dropped, 1);

	const before = calls.length;
	await emitter.tick();
	assert.equal(calls.length, before, 'an idle emitter inside its poll window makes no request');
});

test('a transport failure keeps the batch buffered for the next tick', async () => {
	let clock = Date.UTC(2026, 8, 1, 12, 0, 0);
	let failing = true;
	const emitter = emitterModule.createDeckObserverEmitter({
		readState: () => liveState(),
		now: () => new Date(clock)
	});
	respond = (call) => {
		if (call.url.endsWith('/api/sets/recorder')) {
			return jsonResponse(200, {
				active: true,
				session_id: 's',
				pid: 1,
				owned: true,
				recoverable: false
			});
		}
		if (failing) throw new TypeError('Failed to fetch');
		return jsonResponse(202, { accepted: call.body.snapshots.length, status: {} });
	};

	await emitter.tick();
	assert.equal(emitter.status().buffered, 1);
	assert.equal(emitter.status().posted, 0);
	assert.match(emitter.status().last_error, /Failed to fetch/);

	failing = false;
	clock += 1000;
	await emitter.tick();

	const posts = calls.filter((call) => call.method === 'POST');
	const delivered = posts.at(-1).body.snapshots;
	assert.equal(delivered.length, 2, 'the retry carries the snapshot the failure held back');
	assert.ok(delivered[0].observed_at <= delivered[1].observed_at, 'observed_at is non-decreasing');
	assert.equal(emitter.status().buffered, 0);
	assert.equal(emitter.status().posted, 2);
});

test('an impossible engine state is counted, not posted, and does not stop the run', async () => {
	let broken = true;
	const emitter = emitterModule.createDeckObserverEmitter({
		readState: () => liveState(broken ? { deck: { stable_id: null, audible: true } } : {})
	});
	respond = (call) =>
		call.url.endsWith('/api/sets/recorder')
			? jsonResponse(200, { active: true, session_id: 's', pid: 1, owned: true, recoverable: false })
			: jsonResponse(202, { accepted: 1, status: {} });

	await emitter.tick();
	assert.equal(emitter.status().rejected, 1);
	assert.equal(emitter.status().sampled, 0);
	assert.equal(calls.filter((call) => call.method === 'POST').length, 0);

	broken = false;
	await emitter.tick();
	assert.equal(emitter.status().sampled, 1);
	assert.equal(calls.filter((call) => call.method === 'POST').length, 1);
});

test('a batch never exceeds the 600-snapshot server cap', async () => {
	let clock = Date.UTC(2026, 8, 1, 12, 0, 0);
	const emitter = emitterModule.createDeckObserverEmitter({
		readState: () => liveState(),
		now: () => new Date(clock)
	});
	respond = (call) =>
		call.url.endsWith('/api/sets/recorder')
			? jsonResponse(200, {
					active: true,
					session_id: 'session-one',
					pid: 1,
					owned: true,
					recoverable: false
				})
			: jsonResponse(202, { accepted: 0, status: {} });
	// Bound BEFORE sampling, because a snapshot taken with no recording is now
	// dropped rather than posted -- the server requires `session_id`. This test
	// is about the 600 cap, so it needs samples that are genuinely sendable.
	assert.equal(await emitter.refreshRecorder(), 'emitting');

	for (let i = 0; i < 605; i += 1) {
		emitter.sampleOnce();
		clock += 1000;
	}
	assert.equal(emitter.status().buffered, 605);

	await emitter.flushOnce();
	const first = calls.at(-1).body.snapshots;
	assert.equal(first.length, emitterModule.MAX_BATCH_SNAPSHOTS);
	assert.equal(emitter.status().buffered, 5);

	await emitter.flushOnce();
	assert.equal(calls.at(-1).body.snapshots.length, 5);
	assert.equal(emitter.status().buffered, 0);
	assert.equal(emitter.status().posted, 605);
});

test('a wall clock that jumps backwards skips the sample rather than reordering it', () => {
	let clock = Date.UTC(2026, 8, 1, 12, 0, 0);
	const emitter = emitterModule.createDeckObserverEmitter({
		readState: () => liveState(),
		now: () => new Date(clock)
	});

	emitter.sampleOnce();
	clock -= 5000;
	emitter.sampleOnce();

	assert.equal(emitter.status().sampled, 1);
	assert.equal(emitter.status().clock_regressions, 1);
	assert.equal(emitter.status().buffered, 1);
});

// ----------------------------------------------------------------- wiring ---

test('/performance installs the emitter, so the loop has a real caller', () => {
	const route = readFileSync(
		fileURLToPath(new URL('../../src/routes/performance/+page.svelte', import.meta.url)),
		'utf8'
	);
	assert.match(
		route,
		/import \{ installDeckObserverEmitter \} from '\$lib\/sets\/deck-observer-install'/,
		'the performance route must import the emitter installer'
	);
	assert.match(
		route,
		/installDeckObserverEmitter\(\)/,
		'the performance route must call installDeckObserverEmitter in onMount'
	);
});

test('REC enables the opendj_decks source, or the emitter only ever gets 409s', () => {
	// Both REC buttons (/sets and the /performance rail) start through the
	// input picker, which sends PERFORMANCE_RECORDER_SOURCES.
	const page = readFileSync(
		fileURLToPath(new URL('../../src/routes/sets/+page.svelte', import.meta.url)),
		'utf8'
	);
	assert.match(page, /<RecordInputPicker/);
	const choice = readFileSync(
		fileURLToPath(new URL('../../src/lib/sets/record-input-choice.ts', import.meta.url)),
		'utf8'
	);
	assert.match(choice, /PERFORMANCE_RECORDER_SOURCES[^=]*= \[[^\]]*'opendj_decks'[^\]]*\]/);
});

// ------------------------------------------------------------- audibility ---

test('a deck behind a closed channel fader was not heard, so it is not audible', () => {
	const heard = live.toWireSnapshot(liveState(), new Date());
	assert.equal(heard.decks['1'].audible, true, 'control: an open fader IS audible');

	const preCued = live.toWireSnapshot(liveState({ channel: { fader: 0 } }), new Date());
	assert.equal(preCued.decks['1'].playing, true, 'the deck is still transport-playing');
	assert.equal(
		preCued.decks['1'].audible,
		false,
		'pre-cueing with the fader down must not accrue audible_s'
	);
});

test('a crossfader parked on the other bus means the deck was not heard', () => {
	// assign 'A' is the left bus, so crossfader 1 is fully away from it.
	const away = live.toWireSnapshot(
		liveState({ channel: { assign: 'A' }, mixer: { crossfader: 1 } }),
		new Date()
	);
	assert.equal(
		away.decks['1'].audible,
		false,
		'cos(PI/2) is 6.1e-17, not 0, so an exact-zero test would call this audible'
	);

	const toward = live.toWireSnapshot(
		liveState({ channel: { assign: 'A' }, mixer: { crossfader: 0 } }),
		new Date()
	);
	assert.equal(toward.decks['1'].audible, true, 'control: the same deck IS heard at the A end');
});

test('master volume at zero means nothing was heard on any deck', () => {
	const muted = live.toWireSnapshot(liveState({ mixer: { master: 0 } }), new Date());
	assert.equal(muted.decks['1'].audible, false);
});

test('trim at zero means nothing was heard', () => {
	const muted = live.toWireSnapshot(liveState({ channel: { trim: 0 } }), new Date());
	assert.equal(muted.decks['1'].audible, false);
});

test('a deck the engine never presented is not audible however open the mixer', () => {
	const notPresented = live.toWireSnapshot(liveState({ deck: { audible: false } }), new Date());
	assert.equal(notPresented.decks['1'].audible, false);
	assert.equal(
		live.masterPathGain(liveState({ deck: { audible: false } }), 1) >
			live.SILENCE_GAIN_EPSILON,
		true,
		'the mixer really is open, so this proves the transport half still gates'
	);
});

test('an externally routed deck bypasses the crossfader and master gain', () => {
	// ?extroute= wires the deck straight to a USB pair off the channel fader,
	// so a parked crossfader says nothing about what the room heard.
	const state = liveState({ channel: { assign: 'A' }, mixer: { crossfader: 1, master: 0 } });
	assert.equal(live.deckWasHeard(state, 1), false, 'control: not routed, so it reads silent');
	assert.equal(live.deckWasHeard(state, 1, new Set([1])), true, 'routed, so it is heard');
});

test('extroute parsing takes the deck ids and refuses to invent others', () => {
	assert.deepEqual([...live.externallyRoutedDecks('?extroute=1:1,4:7')].sort(), [1, 4]);
	assert.deepEqual([...live.externallyRoutedDecks('')], [], 'no param means no routed decks');
	assert.deepEqual(
		[...live.externallyRoutedDecks('?extroute=9:1,x:2')],
		[],
		'a deck id outside 1-4 is not a deck and must not enter the set'
	);
});

test('the crossfade law here still matches the one the engine applies', () => {
	// masterPathGain copies `_xfGainFor`, which audio-engine keeps private. If
	// the engine's law changes, this emitter would silently score audibility
	// against the old curve, so pin the two together.
	const engineLaw = engineBlockAfter(
		"function _xfGainFor(assign: CrossfaderAssign, x: number): number {"
	);
	// engineBlockAfter itself refuses an anchor that matches zero or twice, and
	// refuses to return an empty body, so it cannot report green on nothing.
	for (const expression of [
		"if (assign === 'THRU') return 1;",
		'Math.cos((x * Math.PI) / 2)',
		'Math.cos(((1 - x) * Math.PI) / 2)'
	]) {
		assert.ok(
			engineLaw.includes(expression),
			`the engine no longer computes "${expression}"; re-derive masterPathGain`
		);
	}
});

// --------------------------------------------------------- session binding ---

test('buffered snapshots are dropped rather than filed under a new session', async () => {
	let clock = Date.UTC(2026, 8, 1, 12, 0, 0);
	let session = 'session-one';
	let failing = true;
	const emitter = emitterModule.createDeckObserverEmitter({
		readState: () => liveState(),
		now: () => new Date(clock)
	});
	respond = (call) => {
		if (call.url.endsWith('/api/sets/recorder')) {
			return jsonResponse(200, {
				active: true,
				session_id: session,
				pid: 1,
				owned: true,
				recoverable: false
			});
		}
		if (failing) throw new TypeError('Failed to fetch');
		return jsonResponse(202, { accepted: call.body.snapshots.length, status: {} });
	};

	await emitter.tick();
	assert.equal(emitter.status().buffered, 1, 'the failed flush kept its snapshot');

	// The recording is stopped and a different one started while we were down.
	session = 'session-two';
	failing = false;
	clock += 1000;
	await emitter.tick();

	const posts = calls.filter((call) => call.method === 'POST');
	assert.equal(posts.length, 2, 'a second POST was attempted');
	assert.equal(
		posts.at(-1).body.snapshots.length,
		1,
		'only the snapshot taken under session-two is posted'
	);
	assert.equal(emitter.status().session_id, 'session-two');
	assert.equal(emitter.status().dropped, 1, 'the orphaned snapshot is counted, not silent');
	assert.match(
		emitter.status().last_drop_reason,
		/session changed from session-one to session-two/,
		'a later successful flush must not erase the record of the discard'
	);
});

test('an unchanged session keeps its backlog across a transport failure', async () => {
	let clock = Date.UTC(2026, 8, 1, 12, 0, 0);
	let failing = true;
	const emitter = emitterModule.createDeckObserverEmitter({
		readState: () => liveState(),
		now: () => new Date(clock)
	});
	respond = (call) => {
		if (call.url.endsWith('/api/sets/recorder')) {
			return jsonResponse(200, {
				active: true,
				session_id: 'session-one',
				pid: 1,
				owned: true,
				recoverable: false
			});
		}
		if (failing) throw new TypeError('Failed to fetch');
		return jsonResponse(202, { accepted: call.body.snapshots.length, status: {} });
	};

	await emitter.tick();
	failing = false;
	clock += 1000;
	await emitter.tick();

	assert.equal(emitter.status().dropped, 0, 'the same session must not lose its backlog');
	assert.equal(emitter.status().posted, 2);
});

// ----------------------------------------------- audibility, second round ---

test('the final master mute silences every deck, routed or not', () => {
	const state = liveState();
	assert.equal(live.deckWasHeard(state, 1, new Set(), false), true, 'control: unmuted IS heard');
	assert.equal(
		live.deckWasHeard(state, 1, new Set(), true),
		false,
		'the master mute node sits last before the destination'
	);
	assert.equal(
		live.deckWasHeard(state, 1, new Set([1]), true),
		false,
		'an externally routed deck passes through it too, unlike the crossfader'
	);
});

test('a deck with every stem part gained to zero was not heard', () => {
	const roformer = (controls) => ({
		status: 'ready',
		source: 'roformer',
		model: 'mel-band',
		layout: 'roformer2',
		available_controls: ['vocal', 'instrumental'],
		alignment: null,
		controls,
		error: null
	});
	const off = { muted: false, solo: false, gain: 0.5 };
	// 3504858fa feat(mixer): remap EQ dials to stem levels (MIXUX-04) made gain
	// a required key of every stem control, so fixtures carry it explicitly.
	const muted = { muted: true, solo: false, gain: 0.5 };
	// A ready deck always exposes the complete public control record. The
	// layout says which controls are active in its signal path.
	const controls = (vocal, instrumental) => ({
		vocal,
		instrumental,
		drums: off,
		bass: off,
		other: off
	});

	const silent = live.toWireSnapshot(
		liveState({
			stems: roformer(controls(muted, muted))
		}),
		new Date()
	);
	assert.equal(silent.decks['1'].audible, false);

	const audible = live.toWireSnapshot(
		liveState({
			stems: roformer(controls(off, muted))
		}),
		new Date()
	);
	assert.equal(audible.decks['1'].audible, true, 'one live part is still something to hear');
});

test('solo can silence a deck whose controls are mostly unmuted', () => {
	// The case a mute-only test misses, and the reason this calls the engine's
	// own stemPartGains instead of asking "is every control muted": VOCAL is
	// soloed AND muted, so mute zeroes it and solo zeroes the other two, while
	// two of the three controls read unmuted.
	const demucs = (controls) => ({
		status: 'ready',
		source: 'demucs',
		model: 'htdemucs',
		layout: 'demucs4',
		available_controls: ['vocal', 'instrumental', 'drums'],
		alignment: null,
		controls,
		error: null
	});
	const off = { muted: false, solo: false, gain: 0.5 };
	const controls = (vocal) => ({ vocal, instrumental: off, drums: off, bass: off, other: off });

	const soloedAndMuted = demucs(controls(
		// gain is required since 3504858fa (MIXUX-04).
		{ muted: true, solo: true, gain: 0.5 }
	));
	assert.equal(
		live.everyStemPartSilent({ stems: soloedAndMuted }),
		true,
		'mute wins over solo, and solo silences everything it did not select'
	);
	assert.equal(live.toWireSnapshot(liveState({ stems: soloedAndMuted }), new Date()).decks['1'].audible, false);

	// Control: the same solo without the mute is the ordinary isolate, and it
	// is very much heard.
	const soloed = demucs(controls({ muted: false, solo: true, gain: 0.5 }));
	assert.equal(live.everyStemPartSilent({ stems: soloed }), false);
	assert.equal(live.toWireSnapshot(liveState({ stems: soloed }), new Date()).decks['1'].audible, true);
});

test('stem mutes only gate a bundle that is actually in the path', () => {
	const notReady = {
		status: 'unavailable',
		source: null,
		model: null,
		layout: null,
		available_controls: [],
		alignment: null,
		controls: {
			vocal: { muted: true, solo: false, gain: 0.5 },
			instrumental: { muted: true, solo: false, gain: 0.5 },
			drums: { muted: true, solo: false, gain: 0.5 },
			bass: { muted: true, solo: false, gain: 0.5 },
			other: { muted: true, solo: false, gain: 0.5 }
		},
		error: null
	};
	assert.equal(
		live.everyStemPartSilent({ stems: notReady }),
		false,
		'an unavailable bundle is not in the signal path, so its flags say nothing'
	);
	const wire = live.toWireSnapshot(liveState({ stems: notReady }), new Date());
	assert.equal(wire.decks['1'].audible, true, 'and the deck is still heard');
});

// ------------------------------------------- session binding, second round ---

test('a batch carries the session it was sampled under, not the live one', async () => {
	let session = 'session-one';
	const emitter = emitterModule.createDeckObserverEmitter({ readState: () => liveState() });
	respond = (call) =>
		call.url.endsWith('/api/sets/recorder')
			? jsonResponse(200, {
					active: true,
					session_id: session,
					pid: 1,
					owned: true,
					recoverable: false
				})
			: jsonResponse(202, { accepted: 1, status: {} });

	await emitter.tick();
	const post = calls.filter((call) => call.method === 'POST').at(-1);
	assert.equal(
		post.body.session_id,
		'session-one',
		'without this the server cannot tell a stale backlog from a fresh one'
	);
});

// ------------------------------------------- external routing, third round ---

test('with a partial extroute map, an UNMAPPED deck reaches no speaker', () => {
	const state = liveState();
	const mapped = new Set([1]);
	const unmapped = new Set([2]);

	// Control: no external routing at all, so the ordinary master path applies.
	assert.ok(live.masterPathGain(state, 1, new Set()) > live.SILENCE_GAIN_EPSILON);
	assert.equal(live.deckWasHeard(state, 1, new Set(), false), true);

	// Control: deck 1 IS in the map, so it reaches the room over USB.
	assert.ok(live.masterPathGain(state, 1, mapped) > live.SILENCE_GAIN_EPSILON);
	assert.equal(live.deckWasHeard(state, 1, mapped, false), true);

	// Subject: `?extroute=2:1` leaves deck 1 on the internal master, which in
	// that mode feeds only the headphone monitor.
	assert.equal(
		live.masterPathGain(state, 1, unmapped),
		0,
		'_masterGain.connect(_masterMuteGain) runs only in the routing === null branch'
	);
	assert.equal(
		live.deckWasHeard(state, 1, unmapped, false),
		false,
		'headphone-only playback is not a set row'
	);
});

// ------------------------------------------- deferred start, third round ---

test('observing starts at mount; only the POST waits for the boot window', async () => {
	// Replaces an earlier test that asserted the whole observer was deferred.
	// That deferral WAS the defect (Codex #709): with REC already running,
	// bootScheduler can hold the queue ~14s, and the server banks no dwell for
	// a deck's first observation, so a short cue vanished entirely. Sampling is
	// a local read and costs the boot window nothing, so it no longer waits.
	// The teardown assertion is what the old test really protected: no interval
	// outliving the route.
	const realSetInterval = globalThis.setInterval;
	const realClearInterval = globalThis.clearInterval;
	const realWindow = globalThis.window;
	const realDocument = globalThis.document;
	const handles = [];
	const cleared = [];
	globalThis.setInterval = () => {
		const handle = realSetInterval(() => {}, 1_000_000);
		handles.push(handle);
		return handle;
	};
	globalThis.clearInterval = (handle) => {
		cleared.push(handle);
		realClearInterval(handle);
	};
	const fakeHost = () => ({
		location: { search: '' },
		visibilityState: 'visible',
		addEventListener: () => {},
		removeEventListener: () => {}
	});
	let release = null;
	const scheduler = {
		defer: (_name, callback) => {
			release = callback;
		}
	};
	// setTimeout, not setInterval: a zero-interval that is never cleared keeps
	// the event loop alive and the whole FILE times out while every test passes.
	const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

	try {
		globalThis.window = fakeHost();
		globalThis.document = fakeHost();
		respond = (call) =>
			call.url.endsWith('/api/sets/recorder')
				? jsonResponse(200, {
						active: true,
						session_id: 'session-one',
						pid: 1,
						owned: true,
						recoverable: false
					})
				: jsonResponse(202, { accepted: 1, status: {} });

		const teardown = live.installDeckObserverEmitter({ readState: () => liveState() }, scheduler);
		assert.equal(handles.length, 1, 'the sampling timer runs from mount, not from release');

		const observer = globalThis.window[live.DECK_OBSERVER_GLOBAL];
		await settle();
		await observer.tick();
		assert.equal(
			calls.filter((call) => call.method === 'POST').length,
			0,
			'nothing is posted while the boot window is open'
		);
		assert.ok(observer.status().buffered > 0, 'but the snapshots ARE taken and held');

		release();
		await settle();
		assert.ok(
			calls.filter((call) => call.method === 'POST').length >= 1,
			'and the backlog goes out as soon as the window closes'
		);

		teardown();
		assert.deepEqual(cleared, [handles[0]], 'teardown still stops the interval');
	} finally {
		for (const handle of handles) realClearInterval(handle);
		globalThis.setInterval = realSetInterval;
		globalThis.clearInterval = realClearInterval;
		if (realWindow === undefined) delete globalThis.window;
		else globalThis.window = realWindow;
		if (realDocument === undefined) delete globalThis.document;
		else globalThis.document = realDocument;
	}
});
test('leaving the route DURING the boot window still posts what was sampled', async () => {
	// The other half of moving sampling to mount, and the half that was
	// missing: snapshots accumulate from mount while the POST gate is held
	// shut, so a teardown before the window closes had a full buffer and no
	// path that empties it. A client-side route change fires neither
	// `visibilitychange` nor `pagehide`, so the hidden-page handler does not
	// cover this, and the deferred release skips the flush once `disposed` is
	// set. Up to ~14s of a live set, which for a short cue is the whole cue.
	// Codex found it on #709; it is the direct consequence of the split.
	const realSetInterval = globalThis.setInterval;
	const realClearInterval = globalThis.clearInterval;
	const realWindow = globalThis.window;
	const realDocument = globalThis.document;
	const handles = [];
	globalThis.setInterval = () => {
		const handle = realSetInterval(() => {}, 1_000_000);
		handles.push(handle);
		return handle;
	};
	globalThis.clearInterval = (handle) => realClearInterval(handle);
	const fakeHost = () => ({
		location: { search: '' },
		visibilityState: 'visible',
		addEventListener: () => {},
		removeEventListener: () => {}
	});
	// The boot window NEVER closes in this test: the deferred callback is
	// captured and never released, which is exactly the state a user leaving
	// early would find it in.
	const scheduler = { defer: () => {} };
	const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

	try {
		globalThis.window = fakeHost();
		globalThis.document = fakeHost();
		respond = (call) =>
			call.url.endsWith('/api/sets/recorder')
				? jsonResponse(200, {
						active: true,
						session_id: 'session-one',
						pid: 1,
						owned: true,
						recoverable: false
					})
				: jsonResponse(202, { accepted: 1, status: {} });

		const teardown = live.installDeckObserverEmitter({ readState: () => liveState() }, scheduler);
		const observer = globalThis.window[live.DECK_OBSERVER_GLOBAL];
		await settle();
		await observer.tick();

		// SETUP CONTROL: the buffer must genuinely hold something and nothing
		// must have been posted yet, or the assertion below passes vacuously.
		const held = observer.status().buffered;
		assert.ok(held > 0, 'setup: the boot window must have accumulated snapshots');
		assert.equal(
			calls.filter((call) => call.method === 'POST').length,
			0,
			'setup: the gate must still be shut when the route leaves'
		);

		teardown();
		await settle();

		assert.ok(
			calls.filter((call) => call.method === 'POST').length >= 1,
			`teardown inside the boot window lost ${held} buffered snapshot(s)`
		);
	} finally {
		for (const handle of handles) realClearInterval(handle);
		globalThis.setInterval = realSetInterval;
		globalThis.clearInterval = realClearInterval;
		if (realWindow === undefined) delete globalThis.window;
		else globalThis.window = realWindow;
		if (realDocument === undefined) delete globalThis.document;
		else globalThis.document = realDocument;
	}
});

test('a tick whose recorder GET is still in flight at teardown does not post', async () => {
	// `stop()` clears the interval, which cancels every tick that has NOT
	// started and nothing at all about the one that has. `start()` fires an
	// immediate `void tick()`, so a route change during that first recorder GET
	// leaves a continuation that samples a deck engine being disposed - or,
	// after a remount, the NEW engine - and POSTs off-route. An old and a new
	// emitter posting out of timestamp order earns a 422, and the live one then
	// stops observing the rest of the set. Codex found it on #709.
	const realSetInterval = globalThis.setInterval;
	const realClearInterval = globalThis.clearInterval;
	const realWindow = globalThis.window;
	const realDocument = globalThis.document;
	const handles = [];
	globalThis.setInterval = () => {
		const handle = realSetInterval(() => {}, 1_000_000);
		handles.push(handle);
		return handle;
	};
	globalThis.clearInterval = (handle) => realClearInterval(handle);
	const fakeHost = () => ({
		location: { search: '' },
		visibilityState: 'visible',
		addEventListener: () => {},
		removeEventListener: () => {}
	});
	// The boot window is RELEASED here, unlike the test above: this is about a
	// tick surviving teardown, not about the gate.
	let release = () => {};
	const scheduler = { defer: (_name, callback) => { release = callback; } };
	const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
	// The recorder GET is held open by the test, so teardown lands in the exact
	// window the finding describes rather than in a window we hope exists.
	let answerRecorder = () => {};
	const heldRecorder = () =>
		new Promise((resolve) => {
			answerRecorder = () =>
				resolve(
					jsonResponse(200, {
						active: true,
						session_id: 'session-one',
						pid: 1,
						owned: true,
						recoverable: false
					})
				);
		});

	try {
		globalThis.window = fakeHost();
		globalThis.document = fakeHost();
		respond = (call) =>
			call.url.endsWith('/api/sets/recorder')
				? heldRecorder()
				: jsonResponse(202, { accepted: 1, status: {} });

		const teardown = live.installDeckObserverEmitter({ readState: () => liveState() }, scheduler);
		release();
		await settle();

		// SETUP CONTROL: the GET really is unanswered, so the tick really is
		// parked mid-await. Without this the test below passes for a version
		// that never started a tick at all.
		assert.equal(
			calls.filter((call) => call.url.endsWith('/api/sets/recorder')).length,
			1,
			'setup: the immediate tick must have issued its recorder GET'
		);
		assert.equal(
			calls.filter((call) => call.method === 'POST').length,
			0,
			'setup: nothing can have posted while the GET is unanswered'
		);

		teardown();
		await settle();
		const postsAtTeardown = calls.filter((call) => call.method === 'POST').length;

		answerRecorder();
		await settle();
		await settle();

		assert.equal(
			calls.filter((call) => call.method === 'POST').length,
			postsAtTeardown,
			'the resumed tick posted after the route was gone'
		);
	} finally {
		for (const handle of handles) realClearInterval(handle);
		globalThis.setInterval = realSetInterval;
		globalThis.clearInterval = realClearInterval;
		if (realWindow === undefined) delete globalThis.window;
		else globalThis.window = realWindow;
		if (realDocument === undefined) delete globalThis.document;
		else globalThis.document = realDocument;
	}
});

test('a tick whose recorder GET lands while still mounted DOES post', async () => {
	// The control that stops the fix above from overshooting into "the emitter
	// never posts". Same held-GET harness, no teardown: answering the recorder
	// must carry the tick through to a POST. Without this, gating every
	// continuation on `stopped` and gating it on `true` look identical.
	const realSetInterval = globalThis.setInterval;
	const realClearInterval = globalThis.clearInterval;
	const realWindow = globalThis.window;
	const realDocument = globalThis.document;
	const handles = [];
	globalThis.setInterval = () => {
		const handle = realSetInterval(() => {}, 1_000_000);
		handles.push(handle);
		return handle;
	};
	globalThis.clearInterval = (handle) => realClearInterval(handle);
	const fakeHost = () => ({
		location: { search: '' },
		visibilityState: 'visible',
		addEventListener: () => {},
		removeEventListener: () => {}
	});
	let release = () => {};
	const scheduler = { defer: (_name, callback) => { release = callback; } };
	const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
	let answerRecorder = () => {};
	const heldRecorder = () =>
		new Promise((resolve) => {
			answerRecorder = () =>
				resolve(
					jsonResponse(200, {
						active: true,
						session_id: 'session-one',
						pid: 1,
						owned: true,
						recoverable: false
					})
				);
		});

	try {
		globalThis.window = fakeHost();
		globalThis.document = fakeHost();
		respond = (call) =>
			call.url.endsWith('/api/sets/recorder')
				? heldRecorder()
				: jsonResponse(202, { accepted: 1, status: {} });

		const teardown = live.installDeckObserverEmitter({ readState: () => liveState() }, scheduler);
		release();
		await settle();
		assert.equal(
			calls.filter((call) => call.method === 'POST').length,
			0,
			'setup: nothing posts before the recorder answers'
		);

		answerRecorder();
		await settle();
		await settle();

		assert.ok(
			calls.filter((call) => call.method === 'POST').length >= 1,
			'a mounted emitter must still post once the recorder answers'
		);
		teardown();
	} finally {
		for (const handle of handles) realClearInterval(handle);
		globalThis.setInterval = realSetInterval;
		globalThis.clearInterval = realClearInterval;
		if (realWindow === undefined) delete globalThis.window;
		else globalThis.window = realWindow;
		if (realDocument === undefined) delete globalThis.document;
		else globalThis.document = realDocument;
	}
});

// ------------------------------------------ revalidation, fourth round ---

/** Recorder active as `session`, POSTs failing while `postFails`. */
function recorderThenFailingPost(session, state) {
	return (call) => {
		if (call.url.endsWith('/api/sets/recorder')) {
			return state.recorderFails
				? Promise.reject(new TypeError('network down'))
				: jsonResponse(200, {
						active: true,
						session_id: state.session ?? session,
						pid: 1,
						owned: true,
						recoverable: false
					});
		}
		return state.postFails
			? Promise.reject(new TypeError('network down'))
			: jsonResponse(202, { accepted: 1, status: {} });
	};
}

test('a recorder check that failed does not release the backlog', async () => {
	let clock = Date.UTC(2026, 8, 1, 12, 0, 0);
	const state = { postFails: true, recorderFails: false, session: 'session-one' };
	const emitter = emitterModule.createDeckObserverEmitter({
		readState: () => liveState(),
		now: () => new Date(clock)
	});
	respond = recorderThenFailingPost('session-one', state);

	// Tick 1: recorder found, snapshot taken, POST fails -> backlog held and
	// the next tick owes a re-check before it may offer that backlog again.
	await emitter.tick();
	assert.equal(emitter.status().buffered, 1, 'setup: the failed POST kept its batch');

	// Tick 2: the re-check itself fails. An unanswered question is not a yes.
	clock += 1000;
	state.recorderFails = true;
	const postsBefore = calls.filter((c) => c.method === 'POST').length;
	await emitter.tick();
	assert.equal(
		calls.filter((c) => c.method === 'POST').length,
		postsBefore,
		'a failed identity check must not release the backlog'
	);
	assert.ok(emitter.status().buffered >= 1, 'and the backlog is still held, not dropped');

	// Tick 3: the recorder answers, and it is a DIFFERENT session. Now the
	// backlog is dropped on purpose rather than misfiled.
	clock += 1000;
	state.recorderFails = false;
	state.session = 'session-two';
	await emitter.tick();
	assert.match(
		emitter.status().last_drop_reason ?? '',
		/session changed from session-one to session-two/,
		'the deferred check still fires once an answer arrives'
	);
});

test('a tick that overlaps a pending recorder check does nothing', async () => {
	let clock = Date.UTC(2026, 8, 1, 12, 0, 0);
	const state = { postFails: true, recorderFails: false, session: 'session-one' };
	const emitter = emitterModule.createDeckObserverEmitter({
		readState: () => liveState(),
		now: () => new Date(clock)
	});
	respond = recorderThenFailingPost('session-one', state);
	await emitter.tick();
	assert.equal(emitter.status().buffered, 1, 'setup: a backlog exists to be released');

	// Hold the re-check open past the interval, the way a slow engine does.
	let releaseRecorder;
	const held = new Promise((resolve) => {
		releaseRecorder = resolve;
	});
	state.postFails = false;
	respond = (call) =>
		call.url.endsWith('/api/sets/recorder')
			? held.then(() =>
					jsonResponse(200, {
						active: true,
						session_id: 'session-one',
						pid: 1,
						owned: true,
						recoverable: false
					})
				)
			: jsonResponse(202, { accepted: 1, status: {} });

	clock += 1000;
	const first = emitter.tick();
	clock += 1000;
	const second = emitter.tick();
	await second;
	assert.equal(
		calls.filter((c) => c.method === 'POST').length,
		1,
		'the overlapping tick must not post past the check the first is awaiting'
	);

	releaseRecorder();
	await first;
	assert.equal(
		calls.filter((c) => c.method === 'POST').length,
		2,
		'control: once the check answers, the first tick does release the backlog'
	);
});

test('a stale flush returning 409 does not delete the new session\'s samples', async () => {
	let clock = Date.UTC(2026, 8, 1, 12, 0, 0);
	const state = { postFails: true, recorderFails: false, session: 'session-one' };
	const emitter = emitterModule.createDeckObserverEmitter({
		readState: () => liveState(),
		now: () => new Date(clock)
	});
	respond = recorderThenFailingPost('session-one', state);

	// Setup: one snapshot sampled under session-one, POST failed, so the next
	// flush owes a recorder re-read before it may offer that backlog.
	await emitter.tick();
	assert.equal(emitter.status().buffered, 1, 'setup: a session-one backlog exists');

	// A pagehide-style flush starts, captures that backlog, and hangs on the
	// network. It will eventually answer 409.
	let releasePost;
	const heldPost = new Promise((resolve) => {
		releasePost = resolve;
	});
	state.postFails = false;
	respond = (call) =>
		call.url.endsWith('/api/sets/recorder')
			? jsonResponse(200, {
					active: true,
					session_id: state.session,
					pid: 1,
					owned: true,
					recoverable: false
				})
			: heldPost;
	const stale = emitter.flushOnce();

	// While it hangs, the recorder is swapped and a tick samples under the NEW
	// session. This is the interleave: the buffer now holds session-two work
	// that the in-flight request knows nothing about.
	clock += 1000;
	state.session = 'session-two';
	await emitter.tick();
	const afterSwap = emitter.status().buffered;
	assert.equal(afterSwap, 1, 'the stale snapshot went, a fresh one arrived');

	// Now the stale request answers 409.
	releasePost(jsonResponse(409, { detail: 'no live recorder' }));
	await stale;
	assert.equal(
		emitter.status().buffered,
		1,
		'the 409 belongs to the batch it sent, not to snapshots taken since'
	);
});

test('a batch never mixes two sessions, even when the buffer does', async () => {
	let clock = Date.UTC(2026, 8, 1, 12, 0, 0);
	let session = 'session-one';
	const emitter = emitterModule.createDeckObserverEmitter({
		readState: () => liveState(),
		now: () => new Date(clock)
	});
	respond = (call) =>
		call.url.endsWith('/api/sets/recorder')
			? jsonResponse(200, {
					active: true,
					session_id: session,
					pid: 1,
					owned: true,
					recoverable: false
				})
			: jsonResponse(202, { accepted: 1, status: {} });

	// Control first: two samples under ONE session go out together, so the
	// split below cannot be the flusher simply sending one at a time.
	await emitter.refreshRecorder();
	emitter.sampleOnce();
	clock += 1000;
	emitter.sampleOnce();
	await emitter.flushOnce();
	const control = calls.filter((c) => c.method === 'POST').at(-1);
	assert.equal(control.body.snapshots.length, 2, 'control: one session, one batch');
	assert.equal(control.body.session_id, 'session-one');

	// Now a mixed buffer: sampled under one, recorder swapped, sampled under
	// two, with nothing draining in between.
	clock += 1000;
	emitter.sampleOnce();
	clock += 1000;
	session = 'session-two';
	await emitter.refreshRecorder();
	emitter.sampleOnce();
	assert.equal(emitter.status().buffered, 2, 'setup: the buffer holds both sessions');

	await emitter.flushOnce();
	const first = calls.filter((c) => c.method === 'POST').at(-1);
	assert.equal(first.body.snapshots.length, 1, 'the run stops at the session boundary');
	assert.equal(first.body.session_id, 'session-one');

	await emitter.flushOnce();
	const second = calls.filter((c) => c.method === 'POST').at(-1);
	assert.equal(second.body.snapshots.length, 1);
	assert.equal(second.body.session_id, 'session-two', 'and the remainder goes to its own set');
	assert.equal(emitter.status().buffered, 0);
});

test('the re-verification drop removes the stale run only', async () => {
	let clock = Date.UTC(2026, 8, 1, 12, 0, 0);
	const state = { postFails: true, recorderFails: false, session: 'session-one' };
	const emitter = emitterModule.createDeckObserverEmitter({
		readState: () => liveState(),
		now: () => new Date(clock)
	});
	respond = recorderThenFailingPost('session-one', state);

	// A session-one snapshot whose POST failed, so the re-check is armed.
	await emitter.tick();
	assert.equal(emitter.status().buffered, 1);

	// A session-two snapshot lands in the same buffer, through sampleOnce --
	// a real entry point, since the observer is agent-drivable by contract.
	clock += 1000;
	state.session = 'session-two';
	await emitter.refreshRecorder();
	emitter.sampleOnce();
	assert.equal(emitter.status().buffered, 2, 'setup: one snapshot per session');

	// The armed re-check now runs. It must drop the session-one run and leave
	// the session-two one, which belongs to the recording that is live.
	clock += 1000;
	state.postFails = false;
	await emitter.tick();
	assert.match(emitter.status().last_drop_reason ?? '', /while 1 snapshot\(s\) were/);
	const post = calls.filter((c) => c.method === 'POST').at(-1);
	assert.equal(
		post.body.session_id,
		'session-two',
		'the surviving run went to the recorder that is actually running'
	);
	assert.equal(post.body.snapshots.length, 2, 'its own snapshot plus the one this tick took');
});

test('a stale 409 does not forget a recorder discovered while it was in flight', async () => {
	let clock = Date.UTC(2026, 8, 1, 12, 0, 0);
	const state = { postFails: true, recorderFails: false, session: 'session-one' };
	const emitter = emitterModule.createDeckObserverEmitter({
		readState: () => liveState(),
		now: () => new Date(clock)
	});
	respond = recorderThenFailingPost('session-one', state);
	await emitter.tick();
	assert.equal(emitter.status().buffered, 1, 'setup: a session-one backlog exists');

	// A pagehide-style flush captures that backlog and hangs on the network.
	let releasePost;
	const heldPost = new Promise((resolve) => {
		releasePost = resolve;
	});
	state.postFails = false;
	respond = (call) =>
		call.url.endsWith('/api/sets/recorder')
			? jsonResponse(200, {
					active: true,
					session_id: state.session,
					pid: 1,
					owned: true,
					recoverable: false
				})
			: heldPost;
	const stale = emitter.flushOnce();

	// A tick discovers session two while that request is still outstanding.
	clock += 1000;
	state.session = 'session-two';
	await emitter.tick();
	assert.equal(emitter.status().phase, 'emitting');
	assert.equal(emitter.status().session_id, 'session-two', 'setup: the new recorder is known');

	// The stale request now answers 409. It belongs to session one.
	releasePost(jsonResponse(409, { detail: 'no live recorder' }));
	await stale;
	assert.equal(
		emitter.status().phase,
		'emitting',
		'an answer about the previous recording says nothing about this one'
	);
	assert.equal(emitter.status().session_id, 'session-two');

	// And the emitter keeps sampling it rather than waiting out the poll
	// interval to re-learn what it already knew.
	clock += 1000;
	await emitter.tick();
	const post = calls.filter((c) => c.method === 'POST').at(-1);
	assert.equal(post.body.session_id, 'session-two');
});

test('a 409 for the session we still believe in DOES return the emitter to idle', async () => {
	// The control for the guard above: without this, "never reset" would pass
	// that test just as well, and the emitter would poll a dead recorder forever.
	let clock = Date.UTC(2026, 8, 1, 12, 0, 0);
	const emitter = emitterModule.createDeckObserverEmitter({
		readState: () => liveState(),
		now: () => new Date(clock)
	});
	let recorderActive = true;
	respond = (call) =>
		call.url.endsWith('/api/sets/recorder')
			? jsonResponse(200, {
					active: recorderActive,
					session_id: recorderActive ? 'session-one' : null,
					pid: 1,
					owned: true,
					recoverable: false
				})
			: jsonResponse(409, { detail: 'no live recorder' });

	await emitter.tick();
	assert.equal(emitter.status().phase, 'idle', 'the 409 is news about the session we sampled');
	assert.equal(emitter.status().session_id, null);
	assert.ok(emitter.status().dropped >= 1);
});

// -------------------------------------------- the clock floor, sixth round ---

/** The server builds a fresh OpenDjDeckSource per recording (record.py), so
 *  its ordering floor is per SESSION and persists across requests WITHIN one.
 *  These two tests pin both halves of that, and the second is what stops the
 *  fix being "reset the floor whenever convenient". */

test('a clock that regressed between two sets does not refuse the new one', async () => {
	let clock = Date.UTC(2026, 8, 1, 12, 0, 0);
	let session = 'session-one';
	let recorderActive = true;
	const emitter = emitterModule.createDeckObserverEmitter({
		readState: () => liveState(),
		now: () => new Date(clock)
	});
	respond = (call) =>
		call.url.endsWith('/api/sets/recorder')
			? jsonResponse(200, {
					active: recorderActive,
					session_id: recorderActive ? session : null,
					pid: 1,
					owned: true,
					recoverable: false
				})
			: recorderActive
				? jsonResponse(202, { accepted: 1, status: {} })
				: jsonResponse(409, { detail: 'no live recorder' });

	await emitter.tick();
	assert.ok(emitter.status().posted >= 1, 'setup: session one recorded something');

	// The set ends. The 409 belongs to the session we still believe in, so the
	// emitter goes idle and that session's clock floor goes with it.
	recorderActive = false;
	clock += 1000;
	emitter.sampleOnce();
	await emitter.flushOnce();
	assert.equal(emitter.status().phase, 'idle');

	// NTP drags the wall clock back before a new set starts.
	clock -= 30_000;
	recorderActive = true;
	session = 'session-two';
	const regressionsBefore = emitter.status().clock_regressions;
	await emitter.tick();

	assert.equal(
		emitter.status().clock_regressions,
		regressionsBefore,
		'the previous set stopped constraining this one'
	);
	const post = calls.filter((c) => c.method === 'POST').at(-1);
	assert.equal(post.body.session_id, 'session-two');
	assert.equal(post.body.snapshots.length, 1, 'the new set is being recorded, not refused');
});

test('within one session the floor still holds, matching the server', async () => {
	// The control. The server keeps _last_submitted_at across requests inside a
	// session, so relaxing the floor there would send an out-of-order snapshot
	// and earn a 422, which stops the emitter outright.
	let clock = Date.UTC(2026, 8, 1, 12, 0, 0);
	const emitter = emitterModule.createDeckObserverEmitter({
		readState: () => liveState(),
		now: () => new Date(clock)
	});
	respond = (call) =>
		call.url.endsWith('/api/sets/recorder')
			? jsonResponse(200, {
					active: true,
					session_id: 'session-one',
					pid: 1,
					owned: true,
					recoverable: false
				})
			: jsonResponse(202, { accepted: 1, status: {} });

	await emitter.tick();
	assert.ok(emitter.status().posted >= 1, 'setup: a snapshot has been accepted');

	clock -= 30_000;
	const before = emitter.status().clock_regressions;
	await emitter.tick();
	assert.equal(
		emitter.status().clock_regressions,
		before + 1,
		'a backwards clock mid-session is still refused locally, never rewritten'
	);
});

// ------------------------------------------------------------- teardown ---

test('a snapshot taken behind an in-flight POST is not stranded by teardown', async () => {
	// Codex #709. The boot flush (or a `pagehide` flush) is still in flight
	// when the next tick appends a snapshot; /performance then unmounts. The
	// teardown flush returns without sending because a POST is in flight,
	// `stop()` cancels the only future retry, and the in-flight POST removes
	// only its OWN captured entries. That later snapshot is then buffered
	// forever: nothing is dropped, nothing errors, and the set is short by
	// exactly the samples taken during that window.
	const lastTryLeaves = async (lastTry) => {
		let clock = Date.UTC(2026, 8, 1, 12, 0, 0);
		let hold = false;
		let answerHeld = () => {};
		// Resolved when the held POST actually REACHES the harness, which is
		// several microtasks after `flushOnce()` returns its promise: the
		// generated client and the request-body read both await first. Without
		// waiting on this the test released a resolver that did not exist yet
		// and then hung on a POST nobody ever answered.
		let heldReached = () => {};
		const heldStarted = new Promise((resolve) => {
			heldReached = resolve;
		});
		const emitter = emitterModule.createDeckObserverEmitter({
			readState: () => liveState(),
			now: () => new Date(clock),
			recorderPollMs: 60_000
		});
		respond = (call) => {
			if (call.url.endsWith('/api/sets/recorder')) {
				return jsonResponse(200, {
					active: true,
					session_id: 'session-one',
					pid: 1,
					owned: true,
					recoverable: false
				});
			}
			if (!hold) return jsonResponse(202, { accepted: 1, status: {} });
			// Exactly ONE POST is held: `answerHeld` clears the hold as it
			// resolves, so a drain's second pass is answered normally rather
			// than parking on a promise nobody will settle.
			return new Promise((resolve) => {
				answerHeld = () => {
					hold = false;
					resolve(jsonResponse(202, { accepted: 1, status: {} }));
				};
				heldReached();
			});
		};

		await emitter.tick();
		assert.equal(emitter.status().phase, 'emitting', 'setup: the session is bound');

		hold = true;
		clock += 1000;
		emitter.sampleOnce();
		// The boot flush. Started outside a tick, exactly as
		// `deck-observer-install.ts` starts it, and left in flight.
		const bootFlush = emitter.flushOnce();
		await heldStarted;
		clock += 1000;
		await emitter.tick();
		assert.equal(emitter.status().buffered, 2, 'setup: one in flight, one appended behind it');

		// Teardown, in the installer's order: the last try, then stop().
		const last = lastTry(emitter);
		emitter.stop();
		answerHeld();
		await bootFlush;
		await last;
		return emitter.status();
	};

	const drained = await lastTryLeaves((emitter) => emitter.drain());
	assert.equal(drained.buffered, 0, 'drain waits the POST out and then empties what is left');
	assert.equal(drained.posted, 3, 'all three snapshots reached the server');
	assert.equal(drained.dropped, 0, 'and none of them was discarded to get there');

	// CONTROL. The same sequence with the single-shot flush, which is what
	// teardown used to call. If this also came back empty the assertion above
	// would be satisfied by an emitter that never had the defect.
	const flushed = await lastTryLeaves((emitter) => emitter.flushOnce());
	assert.equal(flushed.buffered, 1, 'flushOnce returns early and strands the later snapshot');
	assert.equal(flushed.posted, 2, 'and that snapshot never reaches the server');
});

test('the INSTALLER drains on every exit it has, not just the emitter it wraps', async () => {
	// Found by mutation, not by review: swapping both `emitter.drain()` calls
	// in `deck-observer-install.ts` back to `emitter.flushOnce()` left all 46
	// tests in this file green. The emitter-level test above proves `drain`
	// works; nothing proved the LIFECYCLE calls it, which is the half Codex's
	// finding on #709 is about.
	//
	// Every exit is driven because they are different code paths reached by
	// different events: a client-side route change fires neither
	// `visibilitychange` nor `pagehide` and reaches only the teardown, while a
	// page being frozen or discarded reaches only the visibility handler. The
	// last two share one function today, and driving both is what would notice
	// if they stopped.
	const strandedAfter = async (exit) => {
		const realSetInterval = globalThis.setInterval;
		const realClearInterval = globalThis.clearInterval;
		const realWindow = globalThis.window;
		const realDocument = globalThis.document;
		const handles = [];
		globalThis.setInterval = () => {
			const handle = realSetInterval(() => {}, 1_000_000);
			handles.push(handle);
			return handle;
		};
		const listeners = new Map();
		const fakeHost = () => ({
			location: { search: '' },
			visibilityState: 'visible',
			addEventListener: (name, fn) => listeners.set(name, fn),
			removeEventListener: () => {}
		});
		let openBootWindow = null;
		const scheduler = {
			defer: (_name, callback) => {
				openBootWindow = callback;
			}
		};
		// setTimeout, not setInterval, for the reason the boot-window test
		// gives: a zero-interval nobody clears keeps the loop alive and the
		// whole FILE times out while every test passes.
		const settle = async () => {
			for (let i = 0; i < 12; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
		};

		let hold = false;
		let answerHeld = () => {};
		let heldReached = () => {};
		const heldStarted = new Promise((resolve) => {
			heldReached = resolve;
		});
		try {
			globalThis.window = fakeHost();
			globalThis.document = fakeHost();
			respond = (call) => {
				if (call.url.endsWith('/api/sets/recorder')) {
					return jsonResponse(200, {
						active: true,
						session_id: 'session-one',
						pid: 1,
						owned: true,
						recoverable: false
					});
				}
				if (!hold) return jsonResponse(202, { accepted: 1, status: {} });
				return new Promise((resolve) => {
					answerHeld = () => {
						hold = false;
						resolve(jsonResponse(202, { accepted: 1, status: {} }));
					};
					heldReached();
				});
			};

			const teardown = live.installDeckObserverEmitter(
				{ readState: () => liveState(), recorderPollMs: 60_000 },
				scheduler
			);
			// Captured before teardown, which deletes the global. Agent-native
			// parity is asserted here too: an agent driving this observer needs
			// the verb the lifecycle uses, not only the single-shot one.
			const observer = globalThis.window[live.DECK_OBSERVER_GLOBAL];
			assert.equal(typeof observer.drain, 'function', 'the window global exposes drain');
			await settle();
			await observer.tick();
			assert.ok(observer.status().buffered > 0, 'setup: the boot window is holding snapshots');

			hold = true;
			// The boot window closes and its flush goes out, and is held there.
			openBootWindow();
			await heldStarted;
			await observer.tick();
			assert.ok(observer.status().buffered > 0, 'setup: a snapshot sits behind that POST');

			if (exit === 'teardown') {
				teardown();
			} else {
				globalThis.document.visibilityState = 'hidden';
				listeners.get(exit)();
			}
			answerHeld();
			await settle();
			return observer.status().buffered;
		} finally {
			for (const handle of handles) realClearInterval(handle);
			globalThis.setInterval = realSetInterval;
			globalThis.clearInterval = realClearInterval;
			if (realWindow === undefined) delete globalThis.window;
			else globalThis.window = realWindow;
			if (realDocument === undefined) delete globalThis.document;
			else globalThis.document = realDocument;
		}
	};

	for (const exit of ['teardown', 'visibilitychange', 'pagehide']) {
		assert.equal(
			await strandedAfter(exit),
			0,
			`leaving via ${exit} must wait the in-flight POST out and send what is behind it`
		);
	}
});

test('drain gives up on progress rather than spinning on a refusing server', async () => {
	// The bound is PROGRESS, not a timeout: a pass that removes nothing ends
	// the loop. Without that, a server answering 500 to a buffer that stays
	// full would spin MAX_DRAIN_PASSES times on a page that is being torn
	// down, and a badly written version would not stop at all.
	let clock = Date.UTC(2026, 8, 1, 12, 0, 0);
	const emitter = emitterModule.createDeckObserverEmitter({
		readState: () => liveState(),
		now: () => new Date(clock),
		recorderPollMs: 60_000
	});
	let failing = false;
	respond = (call) => {
		if (call.url.endsWith('/api/sets/recorder')) {
			return jsonResponse(200, {
				active: true,
				session_id: 'session-one',
				pid: 1,
				owned: true,
				recoverable: false
			});
		}
		if (failing) throw new TypeError('Failed to fetch');
		return jsonResponse(202, { accepted: 1, status: {} });
	};

	await emitter.tick();
	failing = true;
	clock += 1000;
	emitter.sampleOnce();
	const postsBefore = calls.filter((call) => call.method === 'POST').length;

	await emitter.drain();

	const attempts = calls.filter((call) => call.method === 'POST').length - postsBefore;
	assert.equal(attempts, 1, 'one wasted pass, not MAX_DRAIN_PASSES of them');
	assert.equal(emitter.status().buffered, 1, 'the snapshot is held, not dropped, and not resent');
	assert.ok(
		emitterModule.MAX_DRAIN_PASSES >= 2,
		'the pass cap only backstops a buffer refilled from elsewhere; progress is the real bound'
	);
});

test('a snapshot bound to no recording is dropped here, not sent to be refused', async () => {
	// `session_id` is REQUIRED on the wire as of this round, so an unbound
	// batch earns a 422 and, before that change, was accepted and filed under
	// whichever set happened to be recording. A tick cannot produce one (it
	// samples only in the emitting phase), but `sampleOnce` is exposed for
	// agents, so the emitter refuses to offer what the server must refuse.
	const emitter = emitterModule.createDeckObserverEmitter({
		readState: () => liveState(),
		recorderPollMs: 60_000
	});
	respond = (call) =>
		call.url.endsWith('/api/sets/recorder')
			? jsonResponse(200, {
					active: true,
					session_id: 'session-one',
					pid: 1,
					owned: true,
					recoverable: false
				})
			: jsonResponse(202, { accepted: 1, status: {} });

	emitter.sampleOnce();
	assert.equal(emitter.status().session_id, null, 'setup: nothing is bound yet');
	await emitter.flushOnce();

	assert.equal(calls.filter((call) => call.method === 'POST').length, 0, 'nothing was posted');
	assert.equal(emitter.status().buffered, 0, 'and the unsendable entry does not park the buffer');
	assert.equal(emitter.status().dropped, 1, 'the loss is counted, never silent');
	assert.match(emitter.status().last_drop_reason, /no recording was bound/);

	// CONTROL: the identical sample, once a recording IS bound, is posted. So
	// the drop above is about the missing session and not about the snapshot.
	assert.equal(await emitter.refreshRecorder(), 'emitting');
	emitter.sampleOnce();
	await emitter.flushOnce();
	assert.equal(calls.filter((call) => call.method === 'POST').length, 1);
	assert.equal(emitter.status().posted, 1);
});

// -------------------------------------------------------- wire contract ---

test('the body this client posts is the body the server tests are run against', async () => {
	// Codex #709 P1. Every other HTTP test in this file is answered by a
	// `globalThis.fetch` that this file wrote, so the generated client and the
	// FastAPI endpoint can drift apart with all of them still green. Node
	// cannot boot the server here (this lane has node, the pytest lane has
	// python), so the two halves are joined by an ARTIFACT rather than by a
	// socket:
	//
	//   - this test drives the real emitter through the real generated client
	//     and asserts the body it produces is the committed fixture. Change
	//     the client and it goes red HERE, in the lane that can run it.
	//   - `tests/sets/test_opendj_api.py::
	//     test_the_body_the_browser_actually_posts_is_accepted_and_recorded`
	//     POSTs that same fixture through the real FastAPI app and asserts a
	//     real recorded row comes out. Change the server and it goes red
	//     THERE, in the lane that can run it.
	//
	// Neither half can pass on a payload nobody sends, which is the property
	// the hand-written `_deck()` dicts in the python suite lack. It is not a
	// live socket and does not claim to be: the transport itself is still
	// covered by the end-to-end run, not by this.
	//
	// Regenerate with MDT_UPDATE_FIXTURES=1 after an INTENDED wire change, and
	// expect the python half to be what judges the new payload.
	const SESSION = '2026-08-31T20-00-00';
	let clock = Date.UTC(2026, 7, 31, 20, 0, 0);
	const emitter = emitterModule.createDeckObserverEmitter({
		readState: () => liveState(),
		now: () => new Date(clock)
	});
	respond = (call) =>
		call.url.endsWith('/api/sets/recorder')
			? jsonResponse(200, {
					active: true,
					session_id: SESSION,
					pid: 1,
					owned: true,
					recoverable: false
				})
			: jsonResponse(202, { accepted: call.body.snapshots.length, status: {} });

	assert.equal(await emitter.refreshRecorder(), 'emitting', 'setup: bound to the session');
	// Long enough to clear the server's 60s advisory dwell at the production
	// cadence, so the python half can assert a real recorded ROW rather than
	// only a 202. That threshold lives in python; the python half asserts this
	// span against it, so a change there fails with an instruction to
	// regenerate rather than silently recording nothing.
	const samples = 65;
	for (let i = 0; i < samples; i += 1) {
		emitter.sampleOnce();
		clock += emitterModule.SNAPSHOT_INTERVAL_MS;
	}
	await emitter.flushOnce();

	const posts = calls.filter((call) => call.method === 'POST');
	assert.equal(posts.length, 1, 'one batch, so the fixture is one whole request');
	assert.equal(posts[0].url, `${API_BASE}/api/sets/deck-observations`);
	const body = posts[0].body;
	assert.equal(body.session_id, SESSION);
	assert.equal(body.snapshots.length, samples);

	const fixture = fileURLToPath(
		new URL('../../../../../tests/fixtures/sets/deck-observations-emitter-post.json', import.meta.url)
	);
	if (process.env.MDT_UPDATE_FIXTURES === '1') {
		writeFileSync(fixture, `${JSON.stringify(body, null, 2)}\n`);
	}
	assert.deepEqual(
		body,
		JSON.parse(readFileSync(fixture, 'utf8')),
		'the committed capture is no longer what this client posts. If the wire change ' +
			'was intended, regenerate with MDT_UPDATE_FIXTURES=1 and let the python half ' +
			'judge the new payload against the real endpoint.'
	);
});
