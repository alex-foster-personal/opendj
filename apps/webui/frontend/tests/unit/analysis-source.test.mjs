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
	analysisSource = await loadTypeScriptModule('src/lib/rb/analysis-source.svelte.ts', {
		viteApiBase: apiBase
	});
});

after(() => {
	serverProcess?.kill();
});

beforeEach(() => {
	analysisSource.analysisSourceState.features = {};
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
