// pack:dir:win must refuse to pack the tracked placeholder payload.
import * as assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';

import { scratchDir } from './helpers';

const SCRIPT = path.join(__dirname, '..', '..', 'scripts', 'require-payload.mjs');
const LAUNCHER = path.join('bin', 'opendj-engine.cmd');
const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'package.json'), 'utf8')) as { scripts: Record<string, string> };

function runCheck(payloadDir: string): { status: number | null; stderr: string } {
	const run = spawnSync(process.execPath, [SCRIPT, payloadDir], { encoding: 'utf8' });
	return { status: run.status, stderr: run.stderr };
}

test('an empty (placeholder) payload stops the pack and names pnpm pack:win and ODJ_PAYLOAD_SRC', () => {
	const payload = scratchDir('payload-empty');
	fs.writeFileSync(path.join(payload, 'README.md'), 'placeholder');
	const run = runCheck(payload);
	assert.notEqual(run.status, 0);
	assert.match(run.stderr, /require-payload: /);
	assert.ok(run.stderr.includes(LAUNCHER), run.stderr);
	assert.match(run.stderr, /pnpm pack:win/);
	assert.match(run.stderr, /ODJ_PAYLOAD_SRC/);
});

test('a payload carrying bin/opendj-engine.cmd passes (the opposite direction)', () => {
	const payload = scratchDir('payload-full');
	fs.mkdirSync(path.join(payload, 'bin'));
	fs.writeFileSync(path.join(payload, LAUNCHER), '@echo off\r\n');
	const run = runCheck(payload);
	assert.equal(run.status, 0, run.stderr);
});

test('a launcher that is a directory is not a launcher', () => {
	const payload = scratchDir('payload-dir-launcher');
	fs.mkdirSync(path.join(payload, LAUNCHER), { recursive: true });
	assert.notEqual(runCheck(payload).status, 0);
});

test('pack:dir:win runs the payload check after tsc and before electron-builder', () => {
	const script = pkg.scripts['pack:dir:win'] ?? '';
	const check = script.indexOf('scripts/require-payload.mjs');
	assert.ok(check !== -1, 'pack:dir:win does not run the payload check');
	assert.ok(script.indexOf('tsc') < check && check < script.indexOf('electron-builder'));
});
