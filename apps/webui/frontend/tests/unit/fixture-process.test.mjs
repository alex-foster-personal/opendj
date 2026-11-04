/**
 * Regression tests for fixture-process.mjs.
 *
 * Read first: `pnpm test:unit` stalled forever on Windows (bifrost2, Tue 22
 * Sep 2026) because `ChildProcess#kill()` on `uv run` ends uv.exe only and
 * the python fixture server it spawned lives on holding the test's stdout
 * pipe. These tests reproduce that shape with a node parent that spawns a
 * node grandchild, so they need no uv or python: the grandchild tests first
 * PROVE the plain kill leaves the grandchild alive (the defect, and the
 * positive control that the liveness probe can see a survivor), then that
 * `killProcessTree` ends it.
 *
 * - if `killProcessTree` leaves a direct child running then broken
 * - if `killProcessTree` on win32 leaves the grandchild running then broken
 * - if `killProcessTree` throws for a process that already exited then broken
 * - if `waitForReadyLine` resolves a port from the matched line then ok, else broken
 * - if `waitForReadyLine` hangs past `timeoutMs` with no ready line then broken
 * - if `waitForReadyLine` hangs when the process exits before printing then broken
 */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { test } from 'node:test';

import { killProcessTree, waitForReadyLine } from './fixture-process.mjs';

const IDLE_FOREVER = 'setInterval(() => {}, 1000)';
/** A parent that spawns an idle grandchild, prints its pid, and idles: the
 * uv.exe -> python.exe shape without either dependency. `detached` matters:
 * libuv puts a non-detached child in the parent's kill-on-close job object,
 * so a node grandchild would die with its parent and the tree walk would
 * pass without being exercised (measured Tue 22 Sep 2026). uv.exe puts
 * python in no job at all, which is why the real fixture survives `kill()`. */
const PARENT_WITH_GRANDCHILD = `
const { spawn } = require('node:child_process');
const child = spawn(process.execPath, ['-e', ${JSON.stringify(IDLE_FOREVER)}], { stdio: 'ignore', detached: true });
console.log('GRANDCHILD ' + child.pid);
${IDLE_FOREVER};
`;
const LIVENESS_DEADLINE_MS = 5000;
const ON_WINDOWS = process.platform === 'win32';
const POSIX_SKIP =
	'tree semantics are measured on win32, where uv.exe cannot exec; POSIX kill() already ends the fixture';

function _spawnNode(script) {
	return spawn(process.execPath, ['-e', script], { stdio: ['ignore', 'pipe', 'inherit'] });
}

function _isAlive(pid) {
	try {
		process.kill(pid, 0);
		return true;
	} catch (err) {
		if (err.code === 'ESRCH') return false;
		throw err;
	}
}

async function _waitUntilDead(pid, label) {
	const deadline = Date.now() + LIVENESS_DEADLINE_MS;
	while (_isAlive(pid)) {
		if (Date.now() > deadline) throw new Error(`${label} pid ${pid} still alive after ${LIVENESS_DEADLINE_MS}ms`);
		await new Promise((resolve) => setTimeout(resolve, 50));
	}
}

test('killProcessTree ends a direct child and the exit is observable', async () => {
	const proc = _spawnNode(IDLE_FOREVER);
	await once(proc, 'spawn');
	killProcessTree(proc);
	await once(proc, 'exit');
	await _waitUntilDead(proc.pid, 'direct child');
});

test('killProcessTree is a no-op for a process that already exited', async () => {
	const proc = _spawnNode('process.exit(0)');
	await once(proc, 'exit');
	assert.equal(proc.exitCode, 0);
	killProcessTree(proc); // must not throw
});

test('plain kill() orphans the grandchild: the defect, and the probe control', { skip: !ON_WINDOWS && POSIX_SKIP }, async () => {
	const proc = _spawnNode(PARENT_WITH_GRANDCHILD);
	const grandchildPid = await waitForReadyLine(proc, /^GRANDCHILD (\d+)$/, {
		timeoutMs: 10_000,
		label: 'parent-with-grandchild'
	});
	assert.ok(_isAlive(grandchildPid), 'grandchild must be alive before the kill');
	proc.kill();
	await once(proc, 'exit');
	await new Promise((resolve) => setTimeout(resolve, 500));
	const survived = _isAlive(grandchildPid);
	// Clean up the orphan whichever way the assertion goes.
	if (survived) process.kill(grandchildPid);
	await _waitUntilDead(grandchildPid, 'orphaned grandchild (cleanup)');
	assert.ok(survived, 'kill() ended the grandchild too: the tree walk in killProcessTree is no longer load-bearing here');
});

test('killProcessTree ends the grandchild uv-style fixtures leave behind', { skip: !ON_WINDOWS && POSIX_SKIP }, async () => {
	const proc = _spawnNode(PARENT_WITH_GRANDCHILD);
	const grandchildPid = await waitForReadyLine(proc, /^GRANDCHILD (\d+)$/, {
		timeoutMs: 10_000,
		label: 'parent-with-grandchild'
	});
	assert.ok(_isAlive(grandchildPid), 'grandchild must be alive before the kill');
	killProcessTree(proc);
	await once(proc, 'exit');
	await _waitUntilDead(grandchildPid, 'grandchild');
});

test('waitForReadyLine resolves the captured port and ignores earlier lines', async () => {
	const proc = _spawnNode(`console.log('warming up'); console.log('READY 4321'); ${IDLE_FOREVER}`);
	try {
		const port = await waitForReadyLine(proc, /^READY (\d+)$/, { timeoutMs: 10_000, label: 'ready-fixture' });
		assert.equal(port, 4321);
	} finally {
		killProcessTree(proc);
		await once(proc, 'exit');
	}
});

test('waitForReadyLine rejects when the process exits before printing ready', async () => {
	const proc = _spawnNode('process.exit(3)');
	await assert.rejects(
		waitForReadyLine(proc, /^READY (\d+)$/, { timeoutMs: 10_000, label: 'early-exit-fixture' }),
		/early-exit-fixture exited early \(3\)/
	);
});

test('waitForReadyLine times out on a silent process and kills its tree', async () => {
	const proc = _spawnNode(IDLE_FOREVER);
	await once(proc, 'spawn');
	await assert.rejects(
		waitForReadyLine(proc, /^READY (\d+)$/, { timeoutMs: 300, label: 'silent-fixture' }),
		/silent-fixture did not print .* within 300ms/
	);
	await once(proc, 'exit');
	await _waitUntilDead(proc.pid, 'silent fixture');
});
