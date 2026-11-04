// [if] the launcher, interpreter or invocation differs by OS without this seam [then] a Windows shell cannot start the engine, [else stop]
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';
import { spawnSync } from 'node:child_process';

import { EngineError } from '../src/shell-log';
import { engineLauncher, launchInvocation, payloadPython, windowsSystemTool } from '../src/platform';
import { scratchDir } from './helpers';

test('the engine launcher is one name per OS family, with the POSIX name unchanged', () => {
	assert.equal(engineLauncher('darwin'), 'bin/opendj-engine');
	assert.equal(engineLauncher('linux'), 'bin/opendj-engine');
	assert.equal(engineLauncher('win32'), 'bin/opendj-engine.cmd');
});

test('the payload interpreter path follows each OS layout', () => {
	assert.equal(payloadPython('/p', 'darwin'), path.join('/p', 'runtime', 'bin', 'python3'));
	assert.equal(payloadPython('/p', 'linux'), path.join('/p', 'runtime', 'bin', 'python3'));
	assert.equal(payloadPython('C:\\p', 'win32'), path.join('C:\\p', 'runtime', 'python.exe'));
});

test('a POSIX launcher is spawned directly, arguments untouched', () => {
	const call = launchInvocation('/p/bin/opendj-engine', ['--port', '1 2'], 'linux');
	assert.deepEqual(call, { command: '/p/bin/opendj-engine', args: ['--port', '1 2'], windowsVerbatimArguments: false });
});

test('a Windows .cmd launcher goes through cmd.exe with every argument quoted once', () => {
	const call = launchInvocation('C:\\App Dir\\bin\\opendj-engine.cmd', ['--data-dir', 'D:\\Music\\A & B\\data'], 'win32', () => 'C:\\Windows\\System32\\cmd.exe');
	assert.equal(call.command, 'C:\\Windows\\System32\\cmd.exe');
	assert.deepEqual(call.args.slice(0, 3), ['/d', '/s', '/c']);
	assert.equal(call.windowsVerbatimArguments, true);
	assert.equal(call.args[3], '""C:\\App Dir\\bin\\opendj-engine.cmd" "--data-dir" "D:\\Music\\A & B\\data""');
});

test('a Windows argument cmd.exe would reinterpret is refused by name, not mangled', () => {
	for (const bad of ['C:\\100%\\data', 'C:\\say "hi"\\data']) {
		assert.throws(
			() => launchInvocation('C:\\p\\bin\\opendj-engine.cmd', ['--data-dir', bad], 'win32'),
			(error: unknown) => error instanceof EngineError && error.detail.includes(bad)
		);
	}
});

test('a .cmd that sits under a path with spaces and an ampersand really runs on Windows', { skip: process.platform !== 'win32' && 'cmd.exe is Windows only' }, () => {
	const root = scratchDir('a & b');
	const file = path.join(root, 'echo args.cmd');
	fs.writeFileSync(file, '@echo off\r\necho \"%~1\"^|\"%~2\"\r\n');
	const call = launchInvocation(file, ['one two', 'x & y'], 'win32');
	const result = spawnSync(call.command, call.args, { encoding: 'utf8', windowsVerbatimArguments: call.windowsVerbatimArguments });
	assert.equal(result.status, 0, result.stderr);
	assert.equal(result.stdout.trim(), '"one two"|"x & y"');
});

test('cmd.exe comes from under the system root, never from PATH', () => {
	const env = { SystemRoot: 'C:\\Windows', Path: 'C:\\evil' };
	assert.equal(windowsSystemTool('cmd.exe', env, () => true), 'C:\\Windows\\System32\\cmd.exe');
	assert.equal(windowsSystemTool('cmd.exe', env, () => false), null);
	assert.equal(windowsSystemTool('cmd.exe', { SystemRoot: 'Windows' }, () => true), null);
	assert.equal(windowsSystemTool('cmd.exe', {}, () => true), null);
});

test('a batch launcher with no trusted cmd.exe is refused, not looked up on PATH', () => {
	assert.throws(
		() => launchInvocation('C:\\p\\bin\\opendj-engine.cmd', [], 'win32', () => null),
		/No cmd\.exe under an absolute SystemRoot/
	);
	const call = launchInvocation('C:\\p\\bin\\opendj-engine.cmd', [], 'win32', () => 'C:\\Windows\\System32\\cmd.exe');
	assert.equal(call.command, 'C:\\Windows\\System32\\cmd.exe');
});
