// Port of the tests in src-tauri/src/shell_health.rs, over real HTTP.
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';

import { SHELL_LOCK_FILE, ShellHealthServer } from '../src/shell-health';
import { scratchDir } from './helpers';

async function call(port: number, method: string, route: string): Promise<{ status: number; body: Record<string, unknown> }> {
	const response = await fetch(`http://127.0.0.1:${port}${route}`, { method });
	return { status: response.status, body: (await response.json()) as Record<string, unknown> };
}

test('the health endpoint reports a dead engine', async () => {
	const dir = scratchDir('shell-health');
	const server = await ShellHealthServer.start(dir, () => '{}');
	try {
		server.update({ status: 'dead', engine: 'dead', lock_pid: 24600, lock_port: 58583, exit_code: 9, reason: null });
		const { status, body } = await call(server.port, 'GET', '/api/v1/health');
		assert.equal(status, 200);
		assert.equal(body.engine, 'dead');
		assert.equal(body.status, 'dead');
		assert.equal(body.exit_code, 9);
		assert.match(String(body.checked_at), /\+00:00$/);
	} finally {
		await server.close();
	}
});

test('.engine.shell.json names the port and this pid, owner-only', async () => {
	const dir = scratchDir('shell-json');
	const server = await ShellHealthServer.start(dir, () => '{}');
	try {
		const file = path.join(dir, SHELL_LOCK_FILE);
		assert.deepEqual(JSON.parse(fs.readFileSync(file, 'utf8')), { health_port: server.port, shell_pid: process.pid });
		assert.equal(fs.statSync(file).mode & 0o777, 0o600);
	} finally {
		await server.close();
	}
});

test('a relaunch request is accepted once and consumed once', async () => {
	const server = await ShellHealthServer.start(scratchDir('relaunch'), () => '{}');
	try {
		assert.equal(server.takeRelaunchRequest(), false);
		const { status, body } = await call(server.port, 'POST', '/api/v1/relaunch');
		assert.equal(status, 202);
		assert.equal(body.status, 'accepted');
		assert.equal(server.takeRelaunchRequest(), true);
		assert.equal(server.takeRelaunchRequest(), false);
	} finally {
		await server.close();
	}
});

test('the audio routes answer from the output-health callback with the right action', async () => {
	const seen: string[] = [];
	const server = await ShellHealthServer.start(scratchDir('audio'), (action) => {
		seen.push(action);
		return JSON.stringify({ action });
	});
	try {
		assert.deepEqual((await call(server.port, 'GET', '/api/v1/audio/output-health')).body, { action: 'probe' });
		assert.deepEqual((await call(server.port, 'POST', '/api/v1/audio/switch-output')).body, { action: 'switch' });
		assert.deepEqual(seen, ['probe', 'switch']);
	} finally {
		await server.close();
	}
});

test('wrong methods and unknown paths are 404, and never trigger a relaunch', async () => {
	const server = await ShellHealthServer.start(scratchDir('404'), () => '{}');
	try {
		assert.equal((await call(server.port, 'GET', '/api/v1/relaunch')).status, 404);
		assert.equal((await call(server.port, 'POST', '/api/v1/health')).status, 404);
		assert.equal((await call(server.port, 'GET', '/nope')).status, 404);
		assert.equal(server.takeRelaunchRequest(), false);
	} finally {
		await server.close();
	}
});
