// requirement: USBIMP-01
// [if] the DJ opens a stick [then] the analysis drain starts only once the folder-import job has succeeded, a failed or cancelled import says so in a toast, and an overlong one says so and is still waited for [⛔️ if the drain starts before the stick's tracks are in the library, or the copy claims the ranking is the analysis order].
//
// Driven against a REAL daemon (fixtures/usb_import_wait_server.py): real
// uvicorn, the production setup, jobs and analysis-queue routers, and a real
// JobRunner that spawns the real setup worker over a temp folder holding a copy
// of one committed acceptance-corpus track. Nothing here scripts a job state:
// every status the code under test reads is one the real job reached.
//
// Not covered here: the IMPORT_WAIT_MS (30 min) still-importing toast. Proving it would mean
// either waiting 30 real minutes or faking the clock.

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { after, before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { resolveUv } from './fixtures/resolve-uv.mjs';
import { stopFixtureServer } from './fixtures/stop-fixture-server.mjs';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const SERVER_SCRIPT = fileURLToPath(new URL('./fixtures/usb_import_wait_server.py', import.meta.url));
const DRAIN = 'POST /api/v1/analysis-queue/run';
const TERMINAL = new Set(['succeeded', 'failed', 'cancelled', 'unknown']);
/** _DRAIN_SETTLE_S in fixtures/usb_import_wait_server.py, read from its source. */
const DRAIN_SETTLE_MS =
	Number(/^_DRAIN_SETTLE_S = ([0-9.]+)$/m.exec(readFileSync(SERVER_SCRIPT, 'utf8'))?.[1] ?? Number.NaN) * 1000;
if (!Number.isFinite(DRAIN_SETTLE_MS)) throw new Error('could not read _DRAIN_SETTLE_S from the fixture server');

let serverProcess;
let apiBase;
let paths;
let usbImport;
let setupApi;

before(async () => {
	serverProcess = spawn(resolveUv(), ['run', '--no-sync', 'python', SERVER_SCRIPT], {
		cwd: REPOSITORY_ROOT,
		env: { ...process.env, PYTHONPATH: REPOSITORY_ROOT },
		stdio: ['ignore', 'pipe', 'pipe']
	});
	let stderr = '';
	serverProcess.stderr.on('data', (chunk) => {
		stderr += chunk;
	});
	const port = await new Promise((resolve, reject) => {
		const lines = createInterface({ input: serverProcess.stdout });
		lines.on('line', (line) => {
			const match = /^READY (\d+)$/.exec(line);
			if (match) resolve(Number(match[1]));
		});
		serverProcess.once('exit', (code) => reject(new Error(`fixture server exited ${code} before READY:\n${stderr}`)));
	});
	apiBase = `http://127.0.0.1:${port}`;
	paths = await (await fetch(`${apiBase}/test/paths`)).json();
	usbImport = await loadTypeScriptModule('src/lib/rb/usb-import.ts', { viteApiBase: apiBase });
	setupApi = await loadTypeScriptModule('src/lib/setup/setup-api.ts', { viteApiBase: apiBase });
});

after(async () => {
	// The server waits up to 120 s (_DRAIN_SETTLE_S) for the real drain it
	// started to finish before exiting, so a 15 s stop bound fails a loaded
	// runner while the server is doing exactly what it should.
	if (serverProcess) await stopFixtureServer(serverProcess, apiBase, { timeoutMs: DRAIN_SETTLE_MS + 15_000 });
});

/** Every API request the real daemon served so far, in order. */
async function served() {
	const res = await fetch(`${apiBase}/test/requests`);
	assert.equal(res.status, 200);
	return res.json();
}

function label(entry) {
	return `${entry.method} ${entry.path}`;
}

describe('USB import waits for the real folder import before analyzing (USBIMP-01)', () => {
	it('drains only after the real folder-import job read succeeded, with the stick track in the backlog', async () => {
		const from = (await served()).length;
		const job = await setupApi.startFolderImport({ folders: [paths.stick] });
		assert.equal(job.kind, 'setup.import-rekordbox');
		assert.ok(!TERMINAL.has(job.status), `a fresh import is not terminal yet: ${job.status}`);

		await usbImport.analyzeWhenImported(job);

		const log = (await served()).slice(from);
		const jobPath = `/api/v1/jobs/${job.id}`;
		const reads = log.filter((e) => e.method === 'GET' && e.path === jobPath);
		const drains = log.filter((e) => label(e) === DRAIN);
		assert.equal(drains.length, 1, JSON.stringify(log));
		assert.equal(drains[0].status, 202, JSON.stringify(drains[0]));

		// The job was really read until it answered succeeded, and the drain came after that read.
		const firstTerminal = log.findIndex((e) => e.path === jobPath && TERMINAL.has(e.job_status));
		assert.ok(firstTerminal >= 0, `no job read answered a terminal status: ${JSON.stringify(log)}`);
		assert.equal(log[firstTerminal].job_status, 'succeeded', JSON.stringify(log[firstTerminal]));
		assert.ok(log.indexOf(drains[0]) > firstTerminal, `drain landed before the terminal read: ${JSON.stringify(log)}`);
		assert.equal(reads.at(-1), log[firstTerminal], 'no job read after the terminal one');
		for (const read of reads.slice(0, -1)) assert.ok(!TERMINAL.has(read.job_status), JSON.stringify(read));

		// The point of waiting: the drain's own backlog already holds the stick's track.
		assert.deepEqual(drains[0].backlog_files, [paths.track]);
	});

	it('never drains when the real folder-import job fails', async () => {
		const from = (await served()).length;
		// The setup route refuses a missing folder before queueing anything (next
		// test), so a job that FAILS in the worker is queued through the jobs API,
		// the same kind and payload shape the setup route builds. A pulled stick
		// is exactly this case: listed at click time, gone when the worker walks it.
		const res = await fetch(`${apiBase}/api/v1/jobs`, {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({
				kind: 'setup.import-rekordbox',
				payload: { data_dir: paths.data_dir, mode: 'folder', roots: [paths.missing_stick] }
			})
		});
		assert.equal(res.status, 201);
		const job = await res.json();
		assert.ok(!TERMINAL.has(job.status), `a fresh import is not terminal yet: ${job.status}`);

		await usbImport.analyzeWhenImported(job);

		const log = (await served()).slice(from);
		const reads = log.filter((e) => e.method === 'GET' && e.path === `/api/v1/jobs/${job.id}`);
		assert.ok(reads.length > 0, `the job was never read: ${JSON.stringify(log)}`);
		assert.equal(reads.at(-1).job_status, 'failed', JSON.stringify(reads));
		const finished = await setupApi.getSetupJob(job.id);
		assert.match(finished.error ?? '', /no such folder/, JSON.stringify(finished));
		assert.deepEqual(log.filter((e) => label(e) === DRAIN), [], JSON.stringify(log));
	});

	it('a stick the setup route refuses up front queues nothing and never drains', async () => {
		const from = (await served()).length;
		await assert.rejects(setupApi.startFolderImport({ folders: [paths.missing_stick] }));
		const log = (await served()).slice(from);
		assert.deepEqual(log.map((e) => [label(e), e.status]), [['POST /api/v1/setup/import/folder', 409]]);
	});

	it('the click path hands the job to the wait instead of draining inline', () => {
		const src = readFileSync(new URL('../../src/lib/rb/usb-import.ts', import.meta.url), 'utf8');
		const body = src.slice(src.indexOf('export async function importUsbVolume'));
		assert.match(body, /job = await startFolderImport\(/);
		assert.match(body, /void analyzeWhenImported\(job\)/);
		assert.doesNotMatch(body, /startAnalysisQueueDrain/);
	});

	it('no copy claims the ranking is the analysis order', () => {
		for (const path of [
			'../../src/lib/rb/usb-import.ts',
			'../../src/lib/rb/usb-import-rank.ts',
			'../../src/lib/components/rb/UsbImportPlan.svelte'
		]) {
			const src = readFileSync(new URL(path, import.meta.url), 'utf8');
			assert.doesNotMatch(src, /analy[sz]ed first|analyze first/, path);
		}
	});
});
