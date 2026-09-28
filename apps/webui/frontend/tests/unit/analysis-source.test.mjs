/**
 * PARITY-02: rbx-vs-own source toggle client (analysis-source.svelte.ts).
 *
 * Regression lines:
 * - if loadAnalysisSource doesn't GET /api/v1/analysis-source and mirror the
 *   response into the reactive state then broken
 * - if setAnalysisSource doesn't PUT {feature, source} and adopt the
 *   response then broken
 * - if a rejected PUT (unknown feature) throws instead of silently keeping
 *   the old local state then broken -- an agent or the UI must see the
 *   failure, never a switch that looks applied but wasn't
 * - if a POLL-detected switch refreshes the decks outside the performance
 *   command queue then broken (discussion_r3968214009): that path is not
 *   already inside a claim, so it would rewrite every deck's grid in the
 *   middle of a PREPARE/START
 * - if setAnalysisSource re-claims those same scopes then broken: its only
 *   caller already holds them, so the claim would wait on its own tail
 * - if two overlapping polls adopt out of order then broken
 *   (discussion_r3968534418)
 * - if a refresh runs with NO runner installed then broken: a missing runner
 *   is a wiring bug and must be loud, never a silent unserialized refresh
 * - if a completed re-analysis (library.changed kind=tracks) leaves an OWN
 *   deck on the superseded grid then broken (discussion_r3968534441)
 */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { stopFixtureServer } from './fixtures/stop-fixture-server.mjs';

const ALL_SOURCE_FEATURES = ['beatgrid', 'key', 'waveform', 'loudness', 'vocal'];

/** Mirror the five-lane GET shape the production endpoint now returns. */
function mirrorFeatures(overrides = {}) {
	return Object.fromEntries(
		ALL_SOURCE_FEATURES.map((feature) => [feature, overrides[feature] ?? 'rekordbox'])
	);
}

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const SERVER_SCRIPT = fileURLToPath(new URL('./fixtures/analysis_source_anlz_server.py', import.meta.url));

let analysisSource;
let serverProcess;
let apiBase;
/** Every entry/exit of the injected command-scheduler runner, in order. */
let runnerLog = [];
/** How many of the next runner invocations must reject, for the retry tests. */
let runnerFailures = 0;
/** The single FakeSocket the shared bus is opened on. See _openSharedBus. */
let busSocket = null;
/** Monotonic sequence for that bus. A repeated or skipped seq is a GAP to the
 * bus, which fires resync - and resync now refreshes too, so a hardcoded seq
 * would make a kind-path test silently measure the resync path instead. */
let busSeq = 0;

/** Minimal stand-in for the browser WebSocket, same shape events-bus.test.mjs
 * drives. The bus only ever assigns handlers and calls close(). */
class FakeSocket {
	constructor(url) {
		this.url = url;
		this.onopen = null;
		this.onmessage = null;
		this.onclose = null;
		this.onerror = null;
	}
	open() {
		this.onopen?.({});
	}
	deliver(topic, seq, payload) {
		this.onmessage?.({
			data: JSON.stringify({ topic, seq, ts: '2026-09-09T10:00:00.000Z', payload })
		});
	}
	/** A frame the bus cannot parse. `_onFrame` fires resync('malformed') and
	 * NO envelope, so this isolates the resync path from the kind path. */
	deliverRaw(data) {
		this.onmessage?.({ data });
	}
	close() {}
}

/** Opens the real events bus on a FakeSocket, once per file, and returns it.
 * The bus is module state inside events-bus.ts and `connect` is a no-op while a
 * socket is live, so a per-test connect would silently hand back nothing. */
function _openSharedBus() {
	if (busSocket !== null) return busSocket;
	const sockets = [];
	analysisSource.connectEventsBus('ws://analysis-source.test/events', {
		socketFactory: (url) => {
			const socket = new FakeSocket(url);
			sockets.push(socket);
			return socket;
		},
		scheduler: { setTimeout: () => 0, clearTimeout: () => {} }
	});
	busSocket = sockets.at(-1);
	assert.ok(busSocket, 'the bus must have opened a socket; a no-op connect proves nothing');
	busSocket.open();
	busSocket.deliver('hello', 0, {
		contract_rev: 'rev-1',
		engine_version: '1.0.0',
		seq_start: 0,
		topics: []
	});
	return busSocket;
}

/** Every URL the real fixture server has served so far, oldest first - the
 * server's own access log, not a spy on the client's fetch. */
async function requestLog() {
	const res = await fetch(`${apiBase}/test/requests`);
	return res.json();
}

/** Polls `currentAnlzFetchGeneration()` until it has moved past `from`, rather
 * than sleeping a guessed duration: a fixed sleep either races a slow shared
 * fixture server under the full suite (too short) or pads every run with
 * dead time (too long, to stay safe). Used to synchronize on a bump this test
 * cannot otherwise observe landing (`refreshAnalysisSourceDecks`'s own bump
 * for a failed switch's deck refresh, before its compensating rollback). */
async function _waitForGenerationPast(from) {
	const deadline = Date.now() + 5000;
	for (;;) {
		const gen = analysisSource.currentAnlzFetchGeneration();
		if (gen > from) return gen;
		if (Date.now() > deadline) throw new Error('fetch generation never advanced past ' + from);
		await new Promise((resolve) => setTimeout(resolve, 5));
	}
}

/** Polls `getAnlzEntry(stableId)` until `predicate(entry)` is true. Uses
 * setImmediate rather than a wall-clock guess so settlement is observed on the
 * next microtask turn; the 5000 ms ceiling matches sibling wait helpers. */
async function _waitForAnlzEntry(stableId, predicate, label) {
	const deadline = Date.now() + 5000;
	for (;;) {
		const entry = analysisSource.getAnlzEntry(stableId);
		if (predicate(entry)) return entry;
		if (Date.now() > deadline) {
			throw new Error(`${label}: timed out with entry status=${entry?.status ?? 'absent'}`);
		}
		await new Promise((resolve) => setImmediate(resolve));
	}
}

async function holdNextAnlz(stableId) {
	const res = await fetch(`${apiBase}/test/hold-next-anlz`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ stable_id: stableId })
	});
	assert.equal(res.status, 200, 'fixture server rejected hold-next-anlz');
}

async function releaseHeldAnlz() {
	const res = await fetch(`${apiBase}/test/release-held-anlz`, { method: 'POST' });
	assert.equal(res.status, 200, 'fixture server rejected release-held-anlz');
}

async function _waitForRunnerLog(expected, label) {
	const deadline = Date.now() + 5000;
	for (;;) {
		if (
			runnerLog.length === expected.length &&
			runnerLog.every((entry, index) => entry === expected[index])
		) {
			return;
		}
		if (Date.now() > deadline) {
			throw new Error(`${label}: timed out with runnerLog=${JSON.stringify(runnerLog)}`);
		}
		await new Promise((resolve) => setTimeout(resolve, 5));
	}
}

/** Flip the DAEMON's selection without going through the module under test, so
 * the module sees it exactly as it would see an agent's direct PUT. */
async function daemonSelect(toggle) {
	const res = await fetch(`${apiBase}/api/v1/analysis/source`, {
		method: 'PUT',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ lane: 'beatgrid', toggle })
	});
	assert.equal(res.status, 200, 'fixture server rejected the direct daemon switch');
}

before(async () => {
	serverProcess = spawn('uv', ['run', '--no-sync', 'python', SERVER_SCRIPT], {
		cwd: REPOSITORY_ROOT,
		// PYTHONPATH, not an editable install: CI's frontend job provisions only
		// requirements.txt (no setuptools-rust build) for this one fixture, so
		// `apps` must resolve from the tree rather than from site-packages.
		env: { ...process.env, MDT_LIBRARY_MODE: 'local', PYTHONPATH: REPOSITORY_ROOT },
		stdio: ['ignore', 'pipe', 'inherit']
	});
	const port = await new Promise((resolve, reject) => {
		const lines = createInterface({ input: serverProcess.stdout });
		serverProcess.once('exit', (code) => reject(new Error(`fixture server exited early (${code})`)));
		lines.on('line', (line) => {
			const match = /^READY (\d+)$/.exec(line);
			if (match) resolve(Number(match[1]));
		});
	});
	apiBase = `http://127.0.0.1:${port}`;
	// Entry fixture, not the module directly: the record-change test needs the
	// SAME events-bus instance `subscribeAnalysisRecordChanges` subscribes to.
	analysisSource = await loadTypeScriptModule('tests/unit/fixtures/analysis-source-entry.ts', {
		viteApiBase: apiBase
	});
	analysisSource.installAnalysisSourceRefreshRunner(async (work) => {
		runnerLog.push('enter');
		if (runnerFailures > 0) {
			runnerFailures -= 1;
			runnerLog.push('threw');
			throw new Error('injected scheduler failure');
		}
		// RETURNS the work's result, exactly as the real runner
		// (`_commandScheduler.run`) does: the caller reads the source /anlz
		// actually served off it. A runner that awaited and dropped it would
		// hand the deck watermark `undefined`.
		const served = await work();
		runnerLog.push('exit');
		return served;
	});
});

after(async () => {
	if (serverProcess) await stopFixtureServer(serverProcess, apiBase);
});

beforeEach(() => {
	// BOTH halves. `deckFeatures` is what decides whether a refresh is needed
	// (see _decksDisagreeWith), so leaving it set from a previous test makes the
	// next test's FIRST sighting look like a change and refresh the decks.
	analysisSource.analysisSourceState.features = {};
	analysisSource.analysisSourceState.deckFeatures = {};
	analysisSource.analysisSourceState.serving = ['beatgrid', 'key'];
	runnerLog = [];
	runnerFailures = 0;
});

test('loadAnalysisSource GETs the production daemon selection and mirrors it into state', async () => {
	await analysisSource.loadAnalysisSource();

	assert.deepEqual(analysisSource.analysisSourceState.features, mirrorFeatures());
	assert.deepEqual(runnerLog, [], 'nothing is loaded, so a first sighting has nothing stale to refresh');
});

test('the very first adopt must not rubber-stamp a deck that is ALREADY loaded (discussion_r3973129047)', async () => {
	await daemonSelect('own');
	// A deck loaded onto a track BEFORE this module's first GET ever settled:
	// its /anlz was fetched under whatever source was effective at load time,
	// which this first-ever adopt has no watermark yet to compare against.
	analysisSource.deckStates[1].stable_id = 'real-track-a-own-grid';
	analysisSource.deckStates[1].anlz = { beatgrid: { beat_count: 0, beats: [] } };
	try {
		await analysisSource.loadAnalysisSource();

		assert.deepEqual(
			runnerLog,
			['enter', 'exit'],
			'an unknown watermark with a deck already loaded must force a real refresh, not trust ' +
				"that the loaded deck already matches the daemon's answer"
		);
		assert.deepEqual(
			analysisSource.analysisSourceState.deckFeatures,
			{ beatgrid: 'own' },
			'the watermark must come from what /anlz actually served the loaded deck, not the daemon intent'
		);
	} finally {
		analysisSource.deckStates[1].stable_id = null;
		analysisSource.deckStates[1].anlz = null;
	}
});

test('setAnalysisSource PUTs the production endpoint and adopts its validated response', async () => {
	await analysisSource.setAnalysisSource('beatgrid', 'own');

	assert.deepEqual(analysisSource.analysisSourceState.features, mirrorFeatures({ beatgrid: 'own' }));
});

test('a production-route rejected feature throws and leaves prior state untouched', async () => {
	analysisSource.analysisSourceState.features = mirrorFeatures();
	analysisSource.analysisSourceState.deckFeatures = { beatgrid: 'rekordbox' };
	await assert.rejects(() => analysisSource.setAnalysisSource('vocals', 'own'));
	assert.deepEqual(analysisSource.analysisSourceState.features, mirrorFeatures());
});

test('a GET begun before a local PUT cannot overwrite the confirmed PUT adoption', async () => {
	await fetch(`${apiBase}/test/delay-next-analysis-source-get`, { method: 'POST' });
	const staleGet = analysisSource.loadAnalysisSource();
	await new Promise((resolve) => setTimeout(resolve, 20));
	await analysisSource.setAnalysisSource('beatgrid', 'own');
	await staleGet;

	assert.deepEqual(
		analysisSource.analysisSourceState.features,
		mirrorFeatures({ beatgrid: 'own' }),
		'a delayed older GET must not replace a newer locally confirmed PUT'
	);
});

// -------------------------------------------------- serialized deck refreshes

test('a poll that finds an external switch refreshes decks INSIDE the command queue', async () => {
	await daemonSelect('rbx');
	await analysisSource.loadAnalysisSource();
	assert.deepEqual(analysisSource.analysisSourceState.features, mirrorFeatures());
	assert.deepEqual(runnerLog, [], 'a first sighting is not a change and must not refresh anything');

	await daemonSelect('own'); // an agent driving the endpoint directly
	await analysisSource.loadAnalysisSource();

	assert.deepEqual(
		runnerLog,
		['enter', 'exit'],
		'the poll refresh must run under the scheduler claim, not beside a PREPARE/START'
	);
	assert.deepEqual(analysisSource.analysisSourceState.features, mirrorFeatures({ beatgrid: 'own' }));
});

test('setAnalysisSource does NOT re-enter the queue its only caller already holds', async () => {
	await daemonSelect('rbx');
	analysisSource.analysisSourceState.features = mirrorFeatures();
	analysisSource.analysisSourceState.deckFeatures = { beatgrid: 'rekordbox' };

	await analysisSource.setAnalysisSource('beatgrid', 'own');

	assert.deepEqual(
		runnerLog,
		[],
		'the analysis_source command already holds [...DECK_IDS, sync]; claiming again waits on its own tail forever'
	);
	assert.deepEqual(analysisSource.analysisSourceState.features, mirrorFeatures({ beatgrid: 'own' }));
});

test('a refresh with no runner installed throws instead of silently skipping the queue', async () => {
	const unwired = await loadTypeScriptModule('src/lib/rb/analysis-source.svelte.ts', {
		viteApiBase: apiBase
	});
	await daemonSelect('rbx');
	await unwired.loadAnalysisSource();
	assert.deepEqual(unwired.analysisSourceState.features, mirrorFeatures());

	await daemonSelect('own');
	await assert.rejects(
		() => unwired.loadAnalysisSource(),
		/no analysis source refresh runner is installed/,
		'a missing runner is a wiring bug and has to be loud'
	);
	assert.deepEqual(
		unwired.analysisSourceState.features,
		mirrorFeatures(),
		'a failed refresh must leave the mirror on the OLD value so the next poll retries'
	);
});

// ------------------------------------------------------ overlapping poll GETs

test('two overlapping polls leave the NEWEST daemon answer in place, not the last to arrive', async () => {
	await daemonSelect('rbx');
	analysisSource.analysisSourceState.features = mirrorFeatures();
	analysisSource.analysisSourceState.deckFeatures = { beatgrid: 'rekordbox' };

	// The delayed GET is issued FIRST and answers 'rbx' (correct at issue time);
	// the second GET is issued after the daemon moved and answers 'own'. Without
	// per-poll sequencing both carry the same mutation number, so the slow one
	// adopts last and the mirror ends up on the pre-switch value while the
	// daemon and the decks are on the new one.
	await fetch(`${apiBase}/test/delay-next-analysis-source-get`, { method: 'POST' });
	const slow = analysisSource.loadAnalysisSource();
	await new Promise((resolve) => setTimeout(resolve, 20));
	await daemonSelect('own');
	const fast = analysisSource.loadAnalysisSource();

	await Promise.all([slow, fast]);

	assert.deepEqual(
		analysisSource.analysisSourceState.features,
		mirrorFeatures({ beatgrid: 'own' }),
		'an older poll response overtook a newer one and reinstated the pre-switch source'
	);
	assert.deepEqual(
		runnerLog,
		['enter', 'exit'],
		'the superseded poll must not spend a second full deck refresh either'
	);
});

// --------------------------------------------- re-analysis under an OWN source

test('a completed re-analysis refreshes the decks while the beatgrid lane is OWN', async () => {
	analysisSource.analysisSourceState.features = mirrorFeatures({ beatgrid: 'own' });
	analysisSource.analysisSourceState.deckFeatures = { beatgrid: 'own' };
	const { socket, unsubscribe } = _subscribedBus();

	_deliverTracksChanged(socket);
	await new Promise((resolve) => setTimeout(resolve, 0));

	assert.deepEqual(
		runnerLog,
		['enter', 'exit'],
		'/anlz derives the OWN grid from the analysis record this event replaced; nothing else would notice'
	);

	// Control, the opposite direction: on rekordbox the payload never reads that
	// table, so a metadata edit must NOT cost every loaded deck a multi-MB refetch.
	runnerLog = [];
	analysisSource.analysisSourceState.features = mirrorFeatures();
	_deliverTracksChanged(socket);
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.deepEqual(runnerLog, [], 'a rekordbox-sourced grid cannot have changed; refreshing is waste');

	unsubscribe();
	_deliverTracksChanged(socket);
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.deepEqual(runnerLog, [], 'the returned unsubscribe must actually detach');
});

// ------------------------------------- resync, retry, drift and the rollback

/** Subscribes the module under test to the ONE shared bus.
 *
 * `connect` returns early once a socket exists (events-bus.ts), so the bus is
 * opened exactly once for the file and every test drives the same socket. Each
 * test still owns its own unsubscribe. */
function _subscribedBus() {
	return { socket: _openSharedBus(), unsubscribe: analysisSource.subscribeAnalysisRecordChanges() };
}

/** The exact frame ingest.py's _refresh_worker publishes on completion, at the
 * next contiguous seq so it travels the KIND path and not the gap path. */
function _deliverTracksChanged(socket) {
	socket.deliver('library.changed', ++busSeq, { kind: 'tracks', ids: [] });
}

test('a bus RESYNC refreshes the OWN grids the missed events could have moved', async () => {
	analysisSource.analysisSourceState.features = mirrorFeatures({ beatgrid: 'own' });
	analysisSource.analysisSourceState.deckFeatures = { beatgrid: 'own' };
	const { socket, unsubscribe } = _subscribedBus('ws://analysis-source-resync.test/events');

	// The bus replays NOTHING on a resync: _fireResync walks the resync
	// listeners only, never the kind listeners. A subscription that handles the
	// kind but not the resync therefore sleeps through the one signal that says
	// "you missed an event" (discussion_r3969942717).
	socket.deliverRaw('this frame cannot be parsed');
	await new Promise((resolve) => setTimeout(resolve, 0));

	assert.deepEqual(
		runnerLog,
		['enter', 'exit'],
		'a resync means events were missed, so an OWN deck must refetch its grid'
	);

	// Control, the opposite direction: on rekordbox the /anlz payload never
	// reads the analysis table, so a resync must NOT cost a multi-MB refetch.
	runnerLog = [];
	analysisSource.analysisSourceState.features = mirrorFeatures();
	socket.deliverRaw('still unparseable');
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.deepEqual(runnerLog, [], 'a rekordbox-sourced grid cannot have gone stale');

	// Disposal covers BOTH subscriptions, not just the kind one.
	analysisSource.analysisSourceState.features = mirrorFeatures({ beatgrid: 'own' });
	unsubscribe();
	socket.deliverRaw('after teardown');
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.deepEqual(runnerLog, [], 'the resync subscription must be disposed with the kind one');
});

test('a FAILED record-change refresh stays pending and the next poll retries it', async () => {
	await daemonSelect('own');
	analysisSource.analysisSourceState.features = mirrorFeatures({ beatgrid: 'own' });
	analysisSource.analysisSourceState.deckFeatures = { beatgrid: 'own' };
	const { socket, unsubscribe } = _subscribedBus('ws://analysis-source-retry.test/events');

	runnerFailures = 1;
	_deliverTracksChanged(socket);
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.deepEqual(runnerLog, ['enter', 'threw'], 'the injected failure must actually have fired');

	// Nothing else can recover this. The refresh did not change the SOURCE, so
	// the daemon still answers own and the decks are still recorded as own -
	// the ordinary change path correctly sees nothing to do, while the deck is
	// sitting on a superseded grid with the shared cache already emptied under
	// it (discussion_r3969942725).
	runnerLog = [];
	await analysisSource.loadAnalysisSource();

	assert.deepEqual(
		runnerLog,
		['enter', 'exit'],
		'a transient failure must not strand the experiment on the old grid forever'
	);

	// And it is not a permanent retry loop: once it succeeds it stops asking.
	runnerLog = [];
	await analysisSource.loadAnalysisSource();
	assert.deepEqual(runnerLog, [], 'a satisfied pending refresh must not re-run on every poll');
	unsubscribe();
});

test('an event-driven refresh records what /anlz actually SERVED, not the stale own intent (discussion_r3973129057)', async () => {
	// The REAL daemon is rbx, but this module's own `features` mirror is stale
	// and still says own (exactly what a switch-back an agent made moments ago,
	// before the next 5s poll, leaves behind). `_anyFeatureIsOwn` reads that
	// stale mirror, so the event-driven refresh still runs - and /anlz resolves
	// rbx-vs-own SERVER-side, so it genuinely serves rbx.
	//
	// SID_NO_OWN_ANALYSIS (no own record seeded, no rekordbox mapping either):
	// with no rekordbox mapping, its bpm provenance entry is wholly ABSENT
	// under this rbx selection (apps.analysis.selection has no track_fields
	// row to report), which keeps this test clear of refreshAnalysisSourceDecks's
	// OWN grid/tempo pairing guard (discussion_r3972264411, widened by
	// discussion_r3976638774) - that guard is a real, independently-tested
	// invariant, not the thing this test is about.
	await daemonSelect('rbx');
	analysisSource.analysisSourceState.features = mirrorFeatures({ beatgrid: 'own' });
	analysisSource.analysisSourceState.deckFeatures = { beatgrid: 'own' };
	analysisSource.deckStates[1].stable_id = 'real-track-c-no-own-analysis';
	const { socket, unsubscribe } = _subscribedBus('ws://analysis-source-served-watermark.test/events');

	try {
		_deliverTracksChanged(socket);
		// A REAL /anlz + /tracks round trip, not the zero-network short-circuit
		// the other record-change tests exercise (their decks are never
		// loaded) - poll until the refresh runner finishes rather than a
		// fixed sleep that races pool load (issue #1820).
		await _waitForRunnerLog(['enter', 'exit'], 'event-driven refresh');
		assert.deepEqual(runnerLog, ['enter', 'exit'], 'the stale own mirror must still trigger the refresh');

		assert.deepEqual(
			analysisSource.analysisSourceState.deckFeatures,
			{ beatgrid: 'rekordbox' },
			'the watermark must record what /anlz actually served, not the stale own intent - ' +
				'discarding the served source here leaves a false own watermark that a later poll ' +
				'compares against itself and never corrects'
		);

		// Control, the opposite direction: the watermark now genuinely agrees
		// with the daemon, so a poll confirming that must not buy a second,
		// redundant multi-MB refresh.
		runnerLog = [];
		await analysisSource.loadAnalysisSource();
		assert.deepEqual(
			runnerLog,
			[],
			'a watermark that already matches the daemon must not cost a redundant refresh'
		);
	} finally {
		unsubscribe();
		analysisSource.deckStates[1].stable_id = null;
	}
});

test('an EARLIER refresh completing must not clear a mark a LATER change set while it was in flight (discussion_r3972154604)', async () => {
	await daemonSelect('own');
	analysisSource.analysisSourceState.features = mirrorFeatures({ beatgrid: 'own' });
	analysisSource.analysisSourceState.deckFeatures = { beatgrid: 'own' };
	// A real ~150ms server delay on /anlz (analysis_source_anlz_server.py),
	// not a fabricated timer: the gap it opens is what lets a SECOND event
	// land while the first refresh is still in flight.
	analysisSource.deckStates[1].stable_id = 'real-track-slow-own-grid';
	const { socket, unsubscribe } = _subscribedBus('ws://analysis-source-generation-race.test/events');

	try {
		_deliverTracksChanged(socket); // refresh A starts, ~150ms in flight
		await _waitForRunnerLog(['enter'], 'refresh A in flight');

		// A second, NEWER change arrives while A is still running. Its own
		// refresh (B) fails immediately, so the only thing that could satisfy
		// it is a refresh that actually started after this point.
		runnerFailures = 1;
		_deliverTracksChanged(socket);
		await _waitForRunnerLog(
			['enter', 'enter', 'threw'],
			'refresh B opened and failed while A still in flight'
		);
		assert.deepEqual(
			runnerLog,
			['enter', 'enter', 'threw'],
			'the second change must have opened, and failed, its own refresh attempt'
		);

		// Let A (started before the second change, and so unable to have seen
		// it) finish on its own.
		await _waitForRunnerLog(
			['enter', 'enter', 'threw', 'exit'],
			'refresh A completion after slow /anlz'
		);
		assert.deepEqual(runnerLog, ['enter', 'enter', 'threw', 'exit'], 'refresh A must now have completed');

		// The moment that matters: A succeeded, but B (the refresh that could
		// have captured the second change) failed. A completing must not have
		// satisfied a mark it started before and cannot have reflected - a poll
		// here must still retry. Without the generation guard, A's unconditional
		// clear on success already cleared the flag when A finished, and this
		// poll would silently do nothing, leaving the deck on a superseded grid.
		runnerLog = [];
		await analysisSource.loadAnalysisSource();
		assert.deepEqual(
			runnerLog,
			['enter', 'exit'],
			'a change that arrived mid-flight, whose own refresh failed, must still be retried even ' +
				'though an earlier refresh that could not have seen it completed successfully'
		);
	} finally {
		unsubscribe();
		analysisSource.deckStates[1].stable_id = null;
		analysisSource.deckStates[1].anlz = null;
	}
});

test('a mirror that drifted from the decks still refreshes them on the next answer', async () => {
	await daemonSelect('own');
	// Exactly the state a daemon switch DURING the scheduler wait leaves behind:
	// the decks came back on rekordbox while this client is still holding the
	// older GET's `own`. Comparing the daemon's next answer against the MIRROR
	// reads own-against-own and never refreshes, so the decks stay on the
	// opposite grid for good (discussion_r3970117741).
	analysisSource.analysisSourceState.features = mirrorFeatures({ beatgrid: 'own' });
	analysisSource.analysisSourceState.deckFeatures = { beatgrid: 'rekordbox' };

	await analysisSource.loadAnalysisSource();

	assert.deepEqual(
		runnerLog,
		['enter', 'exit'],
		'the decks are on the other grid; the comparison must notice that, not the mirror'
	);
	assert.deepEqual(analysisSource.analysisSourceState.deckFeatures, { beatgrid: 'own' });
});

test('a mirror that drifted does NOT buy a refresh the decks do not need', async () => {
	await daemonSelect('rbx');
	analysisSource.analysisSourceState.features = mirrorFeatures({ beatgrid: 'own' });
	analysisSource.analysisSourceState.deckFeatures = { beatgrid: 'rekordbox' };

	await analysisSource.loadAnalysisSource();

	// The overshoot control for the test above: the decks already hold what the
	// daemon is serving, so the only thing that needed correcting was the
	// mirror. A refresh here would be a multi-MB refetch for nothing.
	assert.deepEqual(runnerLog, [], 'the decks already agree with the daemon');
	assert.deepEqual(analysisSource.analysisSourceState.features, mirrorFeatures());
});

// ---------------------------------------- stale-source prefetch eviction

test('a switch with no loaded deck to disagree still evicts a stale prefetched cache entry (discussion_r3973991969 P1 BLOCKING)', async () => {
	// beforeEach resets deckFeatures to {}, so THIS module's next adopt is a
	// genuine "first sighting" (`held === undefined` in _decksDisagreeWith) -
	// with nothing loaded on any deck, that reads as no disagreement whatever
	// the daemon answers, and the fast (no-refresh) path in _adopt is the only
	// one that ever runs. This models an agent flipping the daemon directly
	// between an independent prefetch (library hover, never loaded onto a
	// deck) and this client's very first GET: neither watermark nor any
	// loaded deck can see that race, only the cache entry's own stamp can.
	analysisSource.invalidateAnlzCacheEntry('real-track-a-own-grid');

	// Prefetch under whatever the daemon holds BEFORE this module ever adopts
	// anything - independent of any deck, the way library browsing's
	// ensureAnlz runs.
	await daemonSelect('rbx');
	analysisSource.ensureAnlz('real-track-a-own-grid');
	const deadline = Date.now() + 5000;
	while (analysisSource.getAnlzEntry('real-track-a-own-grid')?.status !== 'ready') {
		if (Date.now() > deadline) throw new Error('prefetch never became ready');
		await new Promise((resolve) => setTimeout(resolve, 5));
	}
	assert.equal(
		analysisSource.getAnlzEntry('real-track-a-own-grid').data.beatgrid_source,
		'rekordbox',
		'the prefetch must be seeded under the pre-switch source, or this test proves nothing'
	);

	// An agent flips the daemon before this module's own first GET ever
	// lands - this is the FIRST adopt this module instance has ever run.
	await daemonSelect('own');
	const before_ = await requestLog();
	await analysisSource.loadAnalysisSource();

	assert.deepEqual(
		runnerLog,
		[],
		'nothing is loaded on any deck, so this must be the fast no-refresh path, not the ' +
			'serialized full refresh - proving the eviction below cannot be riding on that instead'
	);
	assert.equal(
		analysisSource.getAnlzEntry('real-track-a-own-grid'),
		undefined,
		'the prefetched rekordbox-source entry must be evicted, or a deck that later loads this ' +
			'track gets a cache HIT on stale pre-switch bytes even though the toggle already reports own'
	);

	// Prove the eviction is REAL, not merely a local flag: a re-prefetch must
	// reach the real server again, and come back stamped with the new source.
	analysisSource.ensureAnlz('real-track-a-own-grid');
	const deadline2 = Date.now() + 5000;
	while (analysisSource.getAnlzEntry('real-track-a-own-grid')?.status !== 'ready') {
		if (Date.now() > deadline2) throw new Error('re-prefetch never became ready');
		await new Promise((resolve) => setTimeout(resolve, 5));
	}
	const refetches = (await requestLog())
		.slice(before_.length)
		.filter((url) => url.includes('/anlz?'));
	assert.equal(refetches.length, 1, 'the eviction must force a real second network request');
	assert.equal(
		analysisSource.getAnlzEntry('real-track-a-own-grid').data.beatgrid_source,
		'own',
		'the re-fetched entry must carry the new source'
	);
});

test('a first adoption whose value changes invalidates an in-flight prefetch even when nothing was ready to evict (r3975043552 P1 BLOCKING)', async () => {
	analysisSource.invalidateAnlzCacheEntry('real-track-a-own-grid');

	// Seed the daemon under the PRE-switch source, then start a prefetch and
	// hold its real /anlz request on the fixture server until we choose to
	// release it. `_fetchAndPublish` writes 'loading' synchronously before the
	// HTTP round-trip, so the hold gives a deterministic in-flight window
	// instead of a wall-clock poll that races shared-runner scheduling.
	await daemonSelect('rbx');
	await holdNextAnlz('real-track-a-own-grid');
	analysisSource.ensureAnlz('real-track-a-own-grid');
	assert.equal(
		analysisSource.getAnlzEntry('real-track-a-own-grid')?.status,
		'loading',
		'the prefetch must still be in flight for this test to prove anything'
	);

	const prefetchGen = analysisSource.currentAnlzFetchGeneration();

	// Flip the daemon and let THIS module's first-ever adopt see the changed
	// value while that prefetch is still held on the wire. Nothing is loaded on
	// any deck, so this is the fast (no-refresh) `_adopt` path, and
	// `evictAnlzCacheEntriesServingOtherSource` cannot see a 'loading' entry -
	// it does not yet know what source it will resolve to. Only a generation
	// bump stops the settling fetch from publishing stale rekordbox bytes
	// under the toggle's new own answer.
	await daemonSelect('own');
	await analysisSource.loadAnalysisSource();
	await _waitForGenerationPast(prefetchGen);

	await releaseHeldAnlz();

	const entry = await _waitForAnlzEntry(
		'real-track-a-own-grid',
		(e) => {
			if (!e) return true;
			if (e.status === 'loading') return false;
			if (e.status === 'ready' && e.data.beatgrid_source === 'rekordbox') {
				throw new Error(
					'a discarded, superseded fetch must never publish rekordbox bytes - ' +
						'stale response leaked through after the switch to own'
				);
			}
			if (e.status === 'ready' && e.data.beatgrid_source === 'own') return true;
			if (e.status === 'error') return true;
			return false;
		},
		'superseded prefetch settlement'
	);

	assert.equal(
		entry,
		undefined,
		'a discarded, superseded fetch must leave no ready entry - _discardSuperseded ' +
			'should not restart a fetch without a registered consumer'
	);
});

test('a switch with no loaded deck leaves an already-agreeing prefetched entry untouched', async () => {
	analysisSource.invalidateAnlzCacheEntry('real-track-b-own-grid');
	await daemonSelect('own');
	await analysisSource.loadAnalysisSource();

	analysisSource.ensureAnlz('real-track-b-own-grid');
	const deadline = Date.now() + 5000;
	while (analysisSource.getAnlzEntry('real-track-b-own-grid')?.status !== 'ready') {
		if (Date.now() > deadline) throw new Error('prefetch never became ready');
		await new Promise((resolve) => setTimeout(resolve, 5));
	}
	const cachedEntry = analysisSource.getAnlzEntry('real-track-b-own-grid');
	assert.equal(cachedEntry.data.beatgrid_source, 'own');

	// Re-adopting the SAME source (a redundant poll answer, or a genuine
	// no-op switch) must not evict an entry that already agrees.
	await analysisSource.loadAnalysisSource();

	assert.equal(
		analysisSource.getAnlzEntry('real-track-b-own-grid'),
		cachedEntry,
		'an entry already on the effective source must survive a re-adoption of that same source'
	);
});

test('a switch whose deck refresh fails puts the DAEMON back where it found it', async () => {
	await daemonSelect('rbx');
	await analysisSource.loadAnalysisSource();
	assert.deepEqual(analysisSource.analysisSourceState.features, mirrorFeatures());

	// A real loaded deck for a stable_id the server genuinely does not have, so
	// the refresh fails on the production route's real 404 rather than on an
	// injected error. `setAnalysisSource` runs serialize:false (its only caller
	// already holds the claim), so this is the engine's own refresh failing.
	analysisSource.deckStates[1].stable_id = 'slow-absent-track';
	analysisSource.deckStates[1].anlz = { beatgrid: { beat_count: 1, beats: [] } };
	try {
		await assert.rejects(
			() => analysisSource.setAnalysisSource('beatgrid', 'own'),
			'a switch whose decks cannot follow must not resolve as if it worked'
		);

		const daemon = await (await fetch(`${apiBase}/api/v1/analysis/source`)).json();
		assert.equal(
			daemon.lanes.beatgrid.toggle,
			'rbx',
			'the PUT commits before the refresh can reject, so without a compensating ' +
				'write the daemon serves own to fresh loads while every deck stays on ' +
				'rekordbox and every poll re-runs the same failing refresh forever ' +
				'(discussion_r3970117737)'
		);
		assert.deepEqual(
			analysisSource.analysisSourceState.deckFeatures,
			{ beatgrid: 'rekordbox' },
			'nothing was published, so the decks are still recorded where they are'
		);
	} finally {
		analysisSource.deckStates[1].stable_id = null;
		analysisSource.deckStates[1].anlz = null;
	}
});

test('a RETRY after a failed switch rolls back to the original source, not the failed one', async () => {
	await daemonSelect('rbx');
	await analysisSource.loadAnalysisSource();

	// No poll in between: the operator clicks again straight away, so the only
	// record of where the daemon was is the one `setAnalysisSource` keeps. The
	// first switch's own PUT already moved that record to 'own', so a rollback
	// that ignores its compensating PUT's answer leaves it there, and THIS
	// second failure restores the daemon to 'own' - the source the decks never
	// reached (discussion_r3970967286).
	analysisSource.deckStates[1].stable_id = 'slow-absent-track';
	analysisSource.deckStates[1].anlz = { beatgrid: { beat_count: 1, beats: [] } };
	try {
		await assert.rejects(() => analysisSource.setAnalysisSource('beatgrid', 'own'));
		await assert.rejects(() => analysisSource.setAnalysisSource('beatgrid', 'own'));

		const daemon = await (await fetch(`${apiBase}/api/v1/analysis/source`)).json();
		assert.equal(
			daemon.lanes.beatgrid.toggle,
			'rbx',
			'the second rollback must displace the SECOND attempt, and land on the ' +
				'source the decks are actually on'
		);
		assert.deepEqual(analysisSource.analysisSourceState.deckFeatures, { beatgrid: 'rekordbox' });
	} finally {
		analysisSource.deckStates[1].stable_id = null;
		analysisSource.deckStates[1].anlz = null;
	}
});

test('a failed switch never clobbers a concurrent agent-driven HTTP change during rollback (discussion_r3972682728, discussion_r3973129053)', async () => {
	// The rollback is now a single atomic compare-and-set PUT
	// (`expected_toggle`, apps.analysis.selection.compare_and_set_toggle), not
	// a client-side GET followed by an unconditional PUT, so there is no
	// separate GET left to intercept - a monkeypatched `globalThis.fetch` would
	// no longer even observe the real race this test exists to prove
	// (discussion_r3973129053 P1 BLOCKING: "coordinate the timing through the
	// real fixture server instead"). Real server, real concurrent request,
	// coordinated by the fixture server's own real ~150ms SID_SLOW_ABSENT
	// coroutine suspension: an agent's PUT is issued 30ms after the failed
	// switch's own PUT lands (comfortably before its ~150ms-later compensating
	// PUT fires), and the rollback's CAS must see the daemon has moved and
	// stand down rather than overwrite it with the stale pre-switch value.
	await daemonSelect('rbx');
	await analysisSource.loadAnalysisSource();

	analysisSource.deckStates[1].stable_id = 'slow-absent-track';
	analysisSource.deckStates[1].anlz = { beatgrid: { beat_count: 1, beats: [] } };
	try {
		const raceWindow = new Promise((resolve) => setTimeout(resolve, 30)).then(() =>
			// A distinct third value (neither 'rbx', the displaced toggle, nor
			// 'own', this failed switch's own attempted toggle) so a rollback
			// that ignored the race and restored 'rbx' anyway is distinguishable
			// from one that correctly saw and deferred to this agent's write.
			daemonSelect('unset')
		);

		await Promise.all([
			assert.rejects(() => analysisSource.setAnalysisSource('beatgrid', 'own')),
			raceWindow
		]);

		const daemon = await (await fetch(`${apiBase}/api/v1/analysis/source`)).json();
		assert.equal(
			daemon.lanes.beatgrid.toggle,
			'unset',
			'the concurrent agent write must win; a rollback that raced past it would ' +
				'have restored the stale rbx value instead'
		);
	} finally {
		analysisSource.deckStates[1].stable_id = null;
		analysisSource.deckStates[1].anlz = null;
	}
});

test(
	'a failed switch restores what its OWN put displaced, not a stale earlier ' +
		'observation (discussion_r3974235454)',
	async () => {
		// A prior GET/PUT this module saw can be stale by the time it issues its
		// NEXT put: an agent's own direct write can land in between, unseen by
		// this module's poll. If the rollback restored that stale earlier
		// observation instead of the value the switch's OWN put actually
		// displaced, it would put the daemon back somewhere it never was.
		await daemonSelect('rbx');
		await analysisSource.loadAnalysisSource();

		// A concurrent agent write, NOT going through setAnalysisSource, lands
		// after this module's last observation but before its next switch - the
		// exact window `body.previous_toggle` exists to close, since a
		// client-side cache captured before this point cannot see it. 'unset' is
		// distinct from both the earlier observation ('rbx') and the switch
		// below's own attempted value ('own'), so the three cannot be confused.
		await daemonSelect('unset');

		analysisSource.deckStates[1].stable_id = 'slow-absent-track';
		analysisSource.deckStates[1].anlz = { beatgrid: { beat_count: 1, beats: [] } };
		try {
			await assert.rejects(() => analysisSource.setAnalysisSource('beatgrid', 'own'));

			const daemon = await (await fetch(`${apiBase}/api/v1/analysis/source`)).json();
			assert.equal(
				daemon.lanes.beatgrid.toggle,
				'unset',
				'the rollback must restore what this put actually displaced (unset, the ' +
					"concurrent agent's write), not this module's stale earlier " +
					"observation (rbx) from before that write landed"
			);
		} finally {
			analysisSource.deckStates[1].stable_id = null;
			analysisSource.deckStates[1].anlz = null;
		}
	}
);

test(
	'a failed switch invalidates a straggling same-generation prefetch again on rollback ' +
		'(r3975043558 P1 BLOCKING)',
	async () => {
		await daemonSelect('rbx');
		await analysisSource.loadAnalysisSource();

		// Seed a real, valid payload OUTSIDE the failure window below, purely to
		// borrow its exact shape - this test proves the rollback evicts a
		// same-generation READY entry, not that a live fetch can win a race
		// against the fixture server's own ~150ms failure timer. Racing that
		// timer with a real second fetch flaked under full-suite load: a
		// straggler that loses the race lands 'loading' forever by design (the
		// existing generation-mismatch discard never overwrites it), which is
		// indistinguishable from a hang, not a real assertion failure.
		analysisSource.invalidateAnlzCacheEntry('real-track-b-own-grid');
		analysisSource.ensureAnlz('real-track-b-own-grid');
		const seedDeadline = Date.now() + 5000;
		while (analysisSource.getAnlzEntry('real-track-b-own-grid')?.status !== 'ready') {
			if (Date.now() > seedDeadline) throw new Error('seed fetch never became ready');
			await new Promise((resolve) => setTimeout(resolve, 5));
		}
		const seedData = analysisSource.getAnlzEntry('real-track-b-own-grid').data;
		analysisSource.invalidateAnlzCacheEntry('real-track-b-own-grid');

		analysisSource.deckStates[1].stable_id = 'slow-absent-track';
		analysisSource.deckStates[1].anlz = { beatgrid: { beat_count: 1, beats: [] } };
		try {
			const genBeforeSwitch = analysisSource.currentAnlzFetchGeneration();
			const switchOutcome = assert.rejects(() => analysisSource.setAnalysisSource('beatgrid', 'own'));

			// Wait on the GENERATION itself, not a guessed sleep: proceeds the
			// instant `refreshAnalysisSourceDecks`'s own bump for this failed
			// switch attempt has landed, however loaded the shared fixture server
			// is under the full suite - well before the ~150ms slow-absent-track
			// failure that triggers the compensating rollback.
			const genDuringFailure = await _waitForGenerationPast(genBeforeSwitch);

			// Deterministically place a READY entry into the cache at exactly this
			// moment, standing in for ANY same-generation straggler (a retry
			// timer's self-scheduled publish, a library prefetch) that could
			// legitimately settle here. `refreshAnlzCacheEntry` writes
			// synchronously - no network round trip to race against the rollback.
			analysisSource.refreshAnlzCacheEntry('real-track-b-own-grid', seedData);
			assert.equal(
				analysisSource.getAnlzEntry('real-track-b-own-grid')?.status,
				'ready',
				'the seeded entry must land before the assertions below mean anything'
			);

			await switchOutcome; // let the failure AND its compensating rollback finish

			assert.ok(
				analysisSource.currentAnlzFetchGeneration() > genDuringFailure,
				'the compensating rollback must bump the fetch generation AGAIN - stopping there ' +
					'after only the failed switch\'s own bump leaves every straggler issued during ' +
					'the failure window looking current forever'
			);
			assert.equal(
				analysisSource.getAnlzEntry('real-track-b-own-grid'),
				undefined,
				'the rollback must invalidate the failed switch\'s own generation again, or this ' +
					'straggling prefetch (issued under that same generation, settled before the ' +
					'rollback) survives as a reusable ready cache hit for whatever deck loads it ' +
					'next, split from the source rollback just restored'
			);
		} finally {
			analysisSource.deckStates[1].stable_id = null;
			analysisSource.deckStates[1].anlz = null;
		}
	}
);

test(
	'a slower superseded switch must not publish onto the deck or cache after a faster one ' +
		'already won (r3975326238 P1 BLOCKING)',
	async () => {
		await daemonSelect('rbx');
		analysisSource.analysisSourceState.features = mirrorFeatures();
		analysisSource.analysisSourceState.deckFeatures = { beatgrid: 'rekordbox' };
		analysisSource.invalidateAnlzCacheEntry('real-track-slow-own-grid');
		// A sentinel no real server response could ever produce, so any change
		// away from it proves a publish happened.
		analysisSource.deckStates[1].stable_id = 'real-track-slow-own-grid';
		analysisSource.deckStates[1].anlz = { beatgrid: { beat_count: -1, beats: [] } };
		try {
			// Park the slow deck's /anlz on the fixture until we release it, so
			// ordering does not depend on shared-runner wall-clock scheduling.
			await holdNextAnlz('real-track-slow-own-grid');
			const before_ = await requestLog();
			const switchToOwn = analysisSource.setAnalysisSource('beatgrid', 'own');

			// Wait until the real /anlz request reached the server (logged before
			// the route handler runs) while still held at the middleware gate.
			const deadline = Date.now() + 5000;
			for (;;) {
				const seen = (await requestLog()).slice(before_.length);
				if (seen.some((url) => url.includes('/real-track-slow-own-grid/anlz'))) break;
				if (Date.now() > deadline) {
					throw new Error('the slow deck refresh never reached the server');
				}
				await new Promise((resolve) => setTimeout(resolve, 5));
			}

			// No loaded deck disagrees with 'rekordbox' yet - the first switch's
			// refresh has not settled, so `deckFeatures` still reads its pre-race
			// baseline - so this one never fetches anything and wins outright.
			await analysisSource.setAnalysisSource('beatgrid', 'rekordbox');
			await releaseHeldAnlz();
			await switchToOwn; // let the slower, now-superseded switch finish discarding

			assert.equal(
				analysisSource.deckStates[1].anlz?.beatgrid.beat_count,
				-1,
				'the superseded slow switch must not publish onto the deck after a faster switch ' +
					'already restored rekordbox - publishing would silently split the deck from the ' +
					'mirror, with no future poll able to detect it'
			);
			assert.equal(
				analysisSource.analysisSourceState.deckFeatures.beatgrid,
				'rekordbox',
				'the faster switch is what the mirror must report'
			);
			assert.equal(
				analysisSource.getAnlzEntry('real-track-slow-own-grid'),
				undefined,
				'the superseded refresh must not have published into the shared cache either'
			);
		} finally {
			await releaseHeldAnlz();
			analysisSource.deckStates[1].stable_id = null;
			analysisSource.deckStates[1].anlz = null;
			await daemonSelect('own');
		}
	}
);

test(
	'a superseded switch whose /tracks fetch lands after the faster switch reverted the daemon ' +
		'discards quietly instead of throwing (nucbox-wsl-23, run 35731185371)',
	async () => {
		// The test above wins its race with the sibling /tracks/{id} fetch answered
		// under the SAME daemon toggle as /anlz. On a loaded runner the two parallel
		// fetches are served on different sides of the second switch's PUT, and the
		// refresh's cross-source guard fired BEFORE its supersession check: the
		// slower switch, whose answer was going to be discarded anyway, rejected
		// setAnalysisSource with "the two parallel fetches landed on different sides
		// of a source switch". Here the fixture holds GET /tracks/{id} until the
		// faster switch has landed, so that ordering is the only one possible.
		await daemonSelect('rbx');
		analysisSource.analysisSourceState.features = mirrorFeatures();
		analysisSource.analysisSourceState.deckFeatures = { beatgrid: 'rekordbox' };
		analysisSource.invalidateAnlzCacheEntry('real-track-slow-own-grid');
		analysisSource.deckStates[1].stable_id = 'real-track-slow-own-grid';
		analysisSource.deckStates[1].anlz = { beatgrid: { beat_count: -1, beats: [] } };
		const armed = await fetch(`${apiBase}/test/hold-next-track`, {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({ stable_id: 'real-track-slow-own-grid' })
		});
		assert.equal(armed.status, 200, 'fixture server refused to hold the next /tracks fetch');
		try {
			const before_ = await requestLog();
			const switchToOwn = analysisSource.setAnalysisSource('beatgrid', 'own');
			// Both of the refresh's parallel fetches have reached the server (the
			// access log is appended before either handler runs, and before the
			// /tracks one is held); the daemon still reads 'own' for /anlz.
			const deadline = Date.now() + 2000;
			for (;;) {
				const seen = (await requestLog()).slice(before_.length);
				if (
					seen.some((url) => url.includes('/real-track-slow-own-grid/anlz')) &&
					seen.some((url) => url.endsWith('/tracks/real-track-slow-own-grid'))
				) {
					break;
				}
				if (Date.now() > deadline) {
					throw new Error('the slow deck refresh never reached the server');
				}
				await new Promise((resolve) => setTimeout(resolve, 5));
			}
			await new Promise((resolve) => setTimeout(resolve, 5));
			// The faster switch reverts the daemon and wins outright (no loaded deck
			// disagrees with 'rekordbox' yet, so it fetches nothing).
			await analysisSource.setAnalysisSource('beatgrid', 'rekordbox');
			// Only now does the held /tracks/{id} run its handler: bpm provenance
			// comes back from 'rekordbox' while /anlz was served under 'own'.
			const released = await fetch(`${apiBase}/test/release-held-track`, { method: 'POST' });
			assert.equal(released.status, 200);
			await switchToOwn; // superseded: must resolve, discarding, not reject

			assert.equal(
				analysisSource.deckStates[1].anlz?.beatgrid.beat_count,
				-1,
				'the superseded switch must not have published onto the deck'
			);
			assert.equal(
				analysisSource.analysisSourceState.deckFeatures.beatgrid,
				'rekordbox',
				'the faster switch is what the mirror must report'
			);
			assert.equal(
				analysisSource.getAnlzEntry('real-track-slow-own-grid'),
				undefined,
				'the superseded refresh must not have published into the shared cache either'
			);
		} finally {
			await fetch(`${apiBase}/test/release-held-track`, { method: 'POST' });
			analysisSource.deckStates[1].stable_id = null;
			analysisSource.deckStates[1].anlz = null;
			await daemonSelect('own');
		}
	}
);

