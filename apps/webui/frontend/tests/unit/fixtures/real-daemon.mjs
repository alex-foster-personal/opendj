/**
 * Start and stop fixtures/real_daemon_server.py: the production daemon app
 * (`create_app`) on a loopback port over a scratch data dir.
 *
 * Same launch as analysis-source.test.mjs: `uv run --no-sync python` from the
 * repository root with `apps` on PYTHONPATH, and a cooperative stop through
 * POST /test/shutdown (stop-fixture-server.mjs).
 */
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';

import { stopFixtureServer } from './stop-fixture-server.mjs';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../../..', import.meta.url));
const SERVER_SCRIPT = fileURLToPath(new URL('./real_daemon_server.py', import.meta.url));

/**
 * @returns {Promise<{
 *   apiBase: string,
 *   scratchDir: string,
 *   reanalyzeSid: string,
 *   beatgrid: { available: boolean, detail: string },
 *   stop: () => Promise<unknown>
 * }>}
 */
export async function startRealDaemon() {
	const serverProcess = spawn('uv', ['run', '--no-sync', 'python', SERVER_SCRIPT], {
		cwd: REPOSITORY_ROOT,
		env: { ...process.env, MDT_LIBRARY_MODE: 'local', PYTHONPATH: REPOSITORY_ROOT },
		stdio: ['ignore', 'pipe', 'inherit']
	});
	const { port, scratchDir, reanalyzeSid, beatgrid } = await new Promise((resolve, reject) => {
		let announced = null;
		let track = null;
		let capability = null;
		const lines = createInterface({ input: serverProcess.stdout });
		serverProcess.once('exit', (code) => reject(new Error(`real daemon exited early (${code})`)));
		lines.on('line', (line) => {
			const scratch = /^SCRATCH (.+)$/.exec(line);
			if (scratch) announced = scratch[1];
			const imported = /^TRACK ([0-9a-f]+)$/.exec(line);
			if (imported) track = imported[1];
			const grid = /^BEATGRID (ok|unavailable) (.+)$/.exec(line);
			if (grid) capability = { available: grid[1] === 'ok', detail: grid[2] };
			const ready = /^READY (\d+)$/.exec(line);
			if (ready) {
				if (announced === null || track === null || capability === null) {
					reject(new Error('real daemon said READY before SCRATCH, TRACK and BEATGRID'));
				} else {
					resolve({ port: Number(ready[1]), scratchDir: announced, reanalyzeSid: track, beatgrid: capability });
				}
			}
		});
	});
	const apiBase = `http://127.0.0.1:${port}`;
	return { apiBase, scratchDir, reanalyzeSid, beatgrid, stop: () => stopFixtureServer(serverProcess, apiBase) };
}
