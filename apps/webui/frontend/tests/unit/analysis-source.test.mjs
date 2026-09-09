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
	close() {}
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
		await work();
		runnerLog.push('exit');
	});
});

after(() => {
	serverProcess?.kill();
});

beforeEach(() => {
	analysisSource.analysisSourceState.features = {};
	runnerLog = [];
});

test('loadAnalysisSource GETs the production daemon selection and mirrors it into state', async () => {
	await analysisSource.loadAnalysisSource();

	assert.deepEqual(analysisSource.analysisSourceState.features, { beatgrid: 'rekordbox' });
});

test('setAnalysisSource PUTs the production endpoint and adopts its validated response', async () => {
	await analysisSource.setAnalysisSource('beatgrid', 'own');

	assert.deepEqual(analysisSource.analysisSourceState.features, { beatgrid: 'own' });
});

test('a production-route rejected feature throws and leaves prior state untouched', async () => {
	analysisSource.analysisSourceState.features = { beatgrid: 'rekordbox' };
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
	const sockets = [];
	analysisSource.connectEventsBus('ws://analysis-source.test/events', {
		socketFactory: (url) => {
			const socket = new FakeSocket(url);
			sockets.push(socket);
			return socket;
		},
		scheduler: { setTimeout: () => 0, clearTimeout: () => {} }
	});
	const socket = sockets.at(-1);
	socket.open();
	socket.deliver('hello', 0, {
		contract_rev: 'rev-1',
		engine_version: '1.0.0',
		seq_start: 0,
		topics: []
	});
	const unsubscribe = analysisSource.subscribeAnalysisRecordChanges();

	// The exact frame ingest.py's _refresh_worker publishes on completion.
	socket.deliver('library.changed', 1, { kind: 'tracks', ids: [] });
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
	socket.deliver('library.changed', 2, { kind: 'tracks', ids: [] });
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.deepEqual(runnerLog, [], 'a rekordbox-sourced grid cannot have changed; refreshing is waste');

	unsubscribe();
	socket.deliver('library.changed', 3, { kind: 'tracks', ids: [] });
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.deepEqual(runnerLog, [], 'the returned unsubscribe must actually detach');
});
