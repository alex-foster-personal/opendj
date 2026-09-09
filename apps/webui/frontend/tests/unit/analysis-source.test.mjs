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

after(() => {
	serverProcess?.kill();
});

beforeEach(() => {
	// BOTH halves. `deckFeatures` is what decides whether a refresh is needed
	// (see _decksDisagreeWith), so leaving it set from a previous test makes the
	// next test's FIRST sighting look like a change and refresh the decks.
	analysisSource.analysisSourceState.features = {};
	analysisSource.analysisSourceState.deckFeatures = {};
	runnerLog = [];
	runnerFailures = 0;
});

test('loadAnalysisSource GETs the production daemon selection and mirrors it into state', async () => {
	await analysisSource.loadAnalysisSource();

	assert.deepEqual(analysisSource.analysisSourceState.features, { beatgrid: 'rekordbox' });
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

	assert.deepEqual(analysisSource.analysisSourceState.features, { beatgrid: 'own' });
});

test('a production-route rejected feature throws and leaves prior state untouched', async () => {
	analysisSource.analysisSourceState.features = { beatgrid: 'rekordbox' };
	analysisSource.analysisSourceState.deckFeatures = { beatgrid: 'rekordbox' };
	await assert.rejects(() => analysisSource.setAnalysisSource('vocals', 'own'));
	assert.deepEqual(analysisSource.analysisSourceState.features, { beatgrid: 'rekordbox' });
});

test('a GET begun before a local PUT cannot overwrite the confirmed PUT adoption', async () => {
	await fetch(`${apiBase}/test/delay-next-analysis-source-get`, { method: 'POST' });
	const staleGet = analysisSource.loadAnalysisSource();
	await new Promise((resolve) => setTimeout(resolve, 20));
	await analysisSource.setAnalysisSource('beatgrid', 'own');
	await staleGet;

	assert.deepEqual(
		analysisSource.analysisSourceState.features,
		{ beatgrid: 'own' },
		'a delayed older GET must not replace a newer locally confirmed PUT'
	);
});

// -------------------------------------------------- serialized deck refreshes

test('a poll that finds an external switch refreshes decks INSIDE the command queue', async () => {
	await daemonSelect('rbx');
	await analysisSource.loadAnalysisSource();
	assert.deepEqual(analysisSource.analysisSourceState.features, { beatgrid: 'rekordbox' });
	assert.deepEqual(runnerLog, [], 'a first sighting is not a change and must not refresh anything');

	await daemonSelect('own'); // an agent driving the endpoint directly
	await analysisSource.loadAnalysisSource();

	assert.deepEqual(
		runnerLog,
		['enter', 'exit'],
		'the poll refresh must run under the scheduler claim, not beside a PREPARE/START'
	);
	assert.deepEqual(analysisSource.analysisSourceState.features, { beatgrid: 'own' });
});

test('setAnalysisSource does NOT re-enter the queue its only caller already holds', async () => {
	await daemonSelect('rbx');
	analysisSource.analysisSourceState.features = { beatgrid: 'rekordbox' };
	analysisSource.analysisSourceState.deckFeatures = { beatgrid: 'rekordbox' };

	await analysisSource.setAnalysisSource('beatgrid', 'own');

	assert.deepEqual(
		runnerLog,
		[],
		'the analysis_source command already holds [...DECK_IDS, sync]; claiming again waits on its own tail forever'
	);
	assert.deepEqual(analysisSource.analysisSourceState.features, { beatgrid: 'own' });
});

test('a refresh with no runner installed throws instead of silently skipping the queue', async () => {
	const unwired = await loadTypeScriptModule('src/lib/rb/analysis-source.svelte.ts', {
		viteApiBase: apiBase
	});
	await daemonSelect('rbx');
	await unwired.loadAnalysisSource();
	assert.deepEqual(unwired.analysisSourceState.features, { beatgrid: 'rekordbox' });

	await daemonSelect('own');
	await assert.rejects(
		() => unwired.loadAnalysisSource(),
		/no analysis source refresh runner is installed/,
		'a missing runner is a wiring bug and has to be loud'
	);
	assert.deepEqual(
		unwired.analysisSourceState.features,
		{ beatgrid: 'rekordbox' },
		'a failed refresh must leave the mirror on the OLD value so the next poll retries'
	);
});

// ------------------------------------------------------ overlapping poll GETs

test('two overlapping polls leave the NEWEST daemon answer in place, not the last to arrive', async () => {
	await daemonSelect('rbx');
	analysisSource.analysisSourceState.features = { beatgrid: 'rekordbox' };
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
		{ beatgrid: 'own' },
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
	analysisSource.analysisSourceState.features = { beatgrid: 'own' };
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
	analysisSource.analysisSourceState.features = { beatgrid: 'rekordbox' };
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
	analysisSource.analysisSourceState.features = { beatgrid: 'own' };
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
	analysisSource.analysisSourceState.features = { beatgrid: 'rekordbox' };
	socket.deliverRaw('still unparseable');
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.deepEqual(runnerLog, [], 'a rekordbox-sourced grid cannot have gone stale');

	// Disposal covers BOTH subscriptions, not just the kind one.
	analysisSource.analysisSourceState.features = { beatgrid: 'own' };
	unsubscribe();
	socket.deliverRaw('after teardown');
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.deepEqual(runnerLog, [], 'the resync subscription must be disposed with the kind one');
});

test('a FAILED record-change refresh stays pending and the next poll retries it', async () => {
	await daemonSelect('own');
	analysisSource.analysisSourceState.features = { beatgrid: 'own' };
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
	// its bpm provenance never reports `status: 'ok'` for the own lane, which
	// keeps this test clear of refreshAnalysisSourceDecks's OWN grid/tempo
	// pairing guard (discussion_r3972264411) - that guard is a real,
	// independently-tested invariant, not the thing this test is about.
	await daemonSelect('rbx');
	analysisSource.analysisSourceState.features = { beatgrid: 'own' };
	analysisSource.analysisSourceState.deckFeatures = { beatgrid: 'own' };
	analysisSource.deckStates[1].stable_id = 'real-track-c-no-own-analysis';
	const { socket, unsubscribe } = _subscribedBus('ws://analysis-source-served-watermark.test/events');

	try {
		_deliverTracksChanged(socket);
		// A REAL /anlz + /tracks round trip, not the zero-network short-circuit
		// the other record-change tests exercise (their decks are never
		// loaded) - a same-tick 0ms wait is not long enough for real loopback
		// I/O to settle, so this needs actual margin.
		await new Promise((resolve) => setTimeout(resolve, 100));
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
	analysisSource.analysisSourceState.features = { beatgrid: 'own' };
	analysisSource.analysisSourceState.deckFeatures = { beatgrid: 'own' };
	// A real ~150ms server delay on /anlz (analysis_source_anlz_server.py),
	// not a fabricated timer: the gap it opens is what lets a SECOND event
	// land while the first refresh is still in flight.
	analysisSource.deckStates[1].stable_id = 'real-track-slow-own-grid';
	const { socket, unsubscribe } = _subscribedBus('ws://analysis-source-generation-race.test/events');

	try {
		_deliverTracksChanged(socket); // refresh A starts, ~150ms in flight
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.deepEqual(runnerLog, ['enter'], 'refresh A must still be in flight');

		// A second, NEWER change arrives while A is still running. Its own
		// refresh (B) fails immediately, so the only thing that could satisfy
		// it is a refresh that actually started after this point.
		runnerFailures = 1;
		_deliverTracksChanged(socket);
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.deepEqual(
			runnerLog,
			['enter', 'enter', 'threw'],
			'the second change must have opened, and failed, its own refresh attempt'
		);

		// Let A (started before the second change, and so unable to have seen
		// it) finish on its own.
		await new Promise((resolve) => setTimeout(resolve, 250));
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
	analysisSource.analysisSourceState.features = { beatgrid: 'own' };
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
	analysisSource.analysisSourceState.features = { beatgrid: 'own' };
	analysisSource.analysisSourceState.deckFeatures = { beatgrid: 'rekordbox' };

	await analysisSource.loadAnalysisSource();

	// The overshoot control for the test above: the decks already hold what the
	// daemon is serving, so the only thing that needed correcting was the
	// mirror. A refresh here would be a multi-MB refetch for nothing.
	assert.deepEqual(runnerLog, [], 'the decks already agree with the daemon');
	assert.deepEqual(analysisSource.analysisSourceState.features, { beatgrid: 'rekordbox' });
});

test('a switch whose deck refresh fails puts the DAEMON back where it found it', async () => {
	await daemonSelect('rbx');
	await analysisSource.loadAnalysisSource();
	assert.deepEqual(analysisSource.analysisSourceState.features, { beatgrid: 'rekordbox' });

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

