// Shared fixtures for the shell's node:test suite. Everything here is real:
// real directories, real processes, real sockets. Nothing is mocked.

import * as fs from 'node:fs';
import * as http from 'node:http';
import * as os from 'node:os';
import * as path from 'node:path';

/** The fixture sources live beside the TS tests, not in the compiled tree. */
export const FIXTURES = path.resolve(__dirname, '..', '..', 'test', 'fixtures');
export const FAKE_ENGINE = path.join(FIXTURES, 'fake-engine.js');

export function scratchDir(name: string): string {
	return fs.mkdtempSync(path.join(os.tmpdir(), `opendj-electron-${name}-`));
}

/**
 * A payload whose `bin/opendj-engine` runs the fake engine under this Node, in
 * `mode`. Returns the payload dir.
 */
export function fakePayload(root: string, mode = 'healthy', launcher = 'bin/opendj-engine'): string {
	const payload = path.join(root, 'payload');
	const file = path.join(payload, launcher);
	fs.mkdirSync(path.dirname(file), { recursive: true });
	fs.writeFileSync(
		file,
		`#!/bin/sh\nFAKE_ENGINE_MODE=${mode} exec ${JSON.stringify(process.execPath)} ${JSON.stringify(FAKE_ENGINE)} "$@"\n`,
		{ mode: 0o755 }
	);
	return payload;
}

/** A loopback server answering `status` on every path. Resolves with its port. */
export function statusServer(status: number): Promise<{ port: number; close: () => Promise<void> }> {
	const server = http.createServer((_req, res) => {
		res.writeHead(status, { 'content-type': 'application/json' });
		res.end('{}');
	});
	return new Promise((resolve) => {
		server.listen(0, '127.0.0.1', () => {
			const address = server.address() as { port: number };
			resolve({ port: address.port, close: () => new Promise((done) => server.close(() => done())) });
		});
	});
}

export function pidGone(pid: number): boolean {
	try {
		process.kill(pid, 0);
	} catch {
		return true;
	}
	// An unreaped zombie still answers signal 0 but is no longer running.
	try {
		return /^\d+ \(.*\) Z/.test(fs.readFileSync(`/proc/${pid}/stat`, 'utf8'));
	} catch {
		return false;
	}
}

export async function waitFor(predicate: () => boolean | Promise<boolean>, timeoutMs = 5_000): Promise<boolean> {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (await predicate()) return true;
		await new Promise((resolve) => setTimeout(resolve, 25));
	}
	return predicate();
}
