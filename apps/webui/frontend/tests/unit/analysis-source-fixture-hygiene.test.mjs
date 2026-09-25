/**
 * The real analysis-source fixture server must leave the tracked tree as it
 * found it. Opening a state db regenerates a sibling AGENTS.md
 * (apps/shared/state/db.py), so while the server kept its db beside itself in
 * tests/unit/fixtures, every run rewrote a committed AGENTS.md (which then went
 * stale against the live schema) and leaked a db plus its -wal/-shm files.
 *
 * Presence, not absence: the server must ANNOUNCE its scratch dir, the dir must
 * exist while it serves (so "gone afterwards" is not vacuous), and the fixtures
 * listing must be identical after a real start/stop cycle.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync, readdirSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const FIXTURES_DIR = fileURLToPath(new URL('./fixtures/', import.meta.url));
const SERVER_SCRIPT = fileURLToPath(new URL('./fixtures/analysis_source_anlz_server.py', import.meta.url));

function listFixtures() {
	return readdirSync(FIXTURES_DIR).sort();
}

test('a real fixture server start/stop leaves tests/unit/fixtures untouched and removes its scratch dir', async () => {
	const before = listFixtures();
	assert.ok(before.includes('analysis_source_anlz_server.py'), 'positive control: the listing sees the fixtures dir');

	const serverProcess = spawn('uv', ['run', '--no-sync', 'python', SERVER_SCRIPT], {
		cwd: REPOSITORY_ROOT,
		env: { ...process.env, MDT_LIBRARY_MODE: 'local', PYTHONPATH: REPOSITORY_ROOT },
		stdio: ['ignore', 'pipe', 'inherit']
	});
	const exited = new Promise((resolve) => serverProcess.once('exit', (code, signal) => resolve({ code, signal })));
	const scratchDir = await new Promise((resolve, reject) => {
		let announced = null;
		serverProcess.once('exit', (code) => reject(new Error(`fixture server exited early (${code})`)));
		createInterface({ input: serverProcess.stdout }).on('line', (line) => {
			const scratch = /^SCRATCH (.+)$/.exec(line);
			if (scratch) announced = scratch[1];
			if (/^READY \d+$/.test(line)) resolve(announced);
		});
	});

	// Stop the server on EVERY path: a live child keeps this test process alive,
	// so a failed assertion here must not turn into a hang.
	try {
		assert.ok(scratchDir, 'the server must announce its scratch dir before READY');
		assert.ok(!scratchDir.startsWith(REPOSITORY_ROOT), `scratch dir must live outside the repo, got ${scratchDir}`);
		assert.ok(existsSync(`${scratchDir}/state.db`), 'the scratch dir must hold the live db while serving');
	} finally {
		serverProcess.kill();
	}
	const { code, signal } = await exited;
	assert.ok(code === 0 || signal === 'SIGTERM', `server must stop cleanly, got code=${code} signal=${signal}`);

	assert.equal(existsSync(scratchDir), false, `scratch dir ${scratchDir} must be removed on shutdown`);
	assert.deepEqual(listFixtures(), before, 'a fixture run must not add or remove files in tests/unit/fixtures');
});
