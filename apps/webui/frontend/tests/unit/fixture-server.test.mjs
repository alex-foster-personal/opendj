/**
 * Shared fixture-server launch helper (issue #13111).
 *
 * Regression lines:
 * - if fixtureServerLaunch uses a bare `uv` command then broken
 * - if a venv exists but argv omits --no-sync and --python then broken
 * - if uvicorn is missing and assertUvicornImportable is not run then broken
 * - if uvicorn is missing and the error does not name uvicorn then broken
 * - if a real analysis fixture never reaches READY then broken
 */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { after, test } from 'node:test';

import {
	assertUvicornImportable,
	fixtureServerLaunch,
	repoVenvPython
} from '../../scripts/fixture-server.mjs';
import { resolveUv } from './fixtures/resolve-uv.mjs';
import { stopFixtureServer } from './fixtures/stop-fixture-server.mjs';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const SERVER_SCRIPT = fileURLToPath(
	new URL('./fixtures/analysis_source_anlz_server.py', import.meta.url)
);
const started = [];

after(() => {
	for (const proc of started) proc.kill();
});

test('fixtureServerLaunch resolves uv and pins the venv when present', () => {
	const launch = fixtureServerLaunch({ repoRoot: REPOSITORY_ROOT, pythonTarget: SERVER_SCRIPT });
	assert.equal(launch.command, resolveUv());
	assert.notEqual(launch.command, 'uv');
	const venvPython = repoVenvPython(REPOSITORY_ROOT);
	if (venvPython) {
		assert.ok(launch.args.includes('--no-sync'));
		assert.ok(launch.args.includes('--python'));
		assert.equal(launch.args[launch.args.indexOf('--python') + 1], venvPython);
	} else {
		assert.ok(launch.args.includes('--locked'));
		assert.ok(launch.args.includes('dev'));
	}
});

test('assertUvicornImportable names uvicorn when import fails', async () => {
	const launch = fixtureServerLaunch({ repoRoot: REPOSITORY_ROOT, pythonTarget: SERVER_SCRIPT });
	await assert.rejects(
		assertUvicornImportable({
			...launch,
			command: process.execPath,
			args: ['python', '-c', 'import uvicorn']
		}),
		/uvicorn/i
	);
});

test('a real analysis fixture boots through the helper when the venv is provisioned', async (t) => {
	const venvPython = repoVenvPython(REPOSITORY_ROOT);
	if (!venvPython) {
		t.skip('no .venv; run uv sync --locked --extra dev at repo root');
	}
	const launch = fixtureServerLaunch({
		repoRoot: REPOSITORY_ROOT,
		pythonTarget: SERVER_SCRIPT
	});
	await assertUvicornImportable(launch);
	const serverProcess = spawn(launch.command, launch.args, {
		cwd: launch.cwd,
		env: launch.env,
		stdio: ['ignore', 'pipe', 'inherit']
	});
	started.push(serverProcess);
	const port = await new Promise((resolve, reject) => {
		const lines = createInterface({ input: serverProcess.stdout });
		serverProcess.once('exit', (code) =>
			reject(new Error(`fixture server exited early (${code})`))
		);
		lines.on('line', (line) => {
			const match = /^READY (\d+)$/.exec(line);
			if (match) resolve(Number(match[1]));
		});
	});
	const apiBase = `http://127.0.0.1:${port}`;
	const res = await fetch(`${apiBase}/api/v1/analysis/source`);
	assert.equal(res.status, 200);
	await stopFixtureServer(serverProcess, apiBase);
	assert.equal(existsSync(venvPython), true);
});
