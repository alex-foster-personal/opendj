// The engine data dir rule per OS (ADR windows-data-dir-and-app-identity).
// Pure-function cases pass the platform explicitly; the child-process cases run
// the compiled module under a real environment, so they exercise the actual
// process.env, not a stand-in.
import * as assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import * as os from 'node:os';
import * as path from 'node:path';
import { test } from 'node:test';

import { DataDirError, resolveEngineDataDir } from '../src/data-dir';

const ID = 'com.opendj.desktop';
const MODULE_JS = path.resolve(__dirname, '..', 'src', 'data-dir.js');

/** A Windows path built from parts, so no test string depends on backslash escaping. */
function win(...parts: string[]): string {
	return parts.join(String.fromCharCode(92));
}
const LOCAL = win('C:', 'Users', 'dj', 'AppData', 'Local');
const ROAMING = win('C:', 'Users', 'dj', 'AppData', 'Roaming');

test('windows resolves to LOCALAPPDATA/<identifier>, never the Roaming appData dir', () => {
	const dir = resolveEngineDataDir(ID, {
		platform: 'win32',
		env: { LOCALAPPDATA: LOCAL, APPDATA: ROAMING },
		homedir: win('C:', 'Users', 'dj'),
		appDataDir: ROAMING
	});
	assert.equal(dir, win(LOCAL, ID));
	assert.ok(!dir.includes('Roaming'), `${dir} is under Roaming`);
});

for (const bad of [undefined, '', '   ']) {
	test(`windows with LOCALAPPDATA=${JSON.stringify(bad)} throws DataDirError naming the variable`, () => {
		assert.throws(
			() => resolveEngineDataDir(ID, { platform: 'win32', env: { LOCALAPPDATA: bad }, homedir: 'C:', appDataDir: ROAMING }),
			(error: unknown) => error instanceof DataDirError && /LOCALAPPDATA/.test(error.message)
		);
	});
}

test('windows with a relative LOCALAPPDATA throws instead of resolving against the cwd', () => {
	assert.throws(
		() => resolveEngineDataDir(ID, { platform: 'win32', env: { LOCALAPPDATA: win('AppData', 'Local') }, homedir: 'C:', appDataDir: 'x' }),
		DataDirError
	);
});

test('macOS keeps <appData>/<identifier> byte for byte', () => {
	const dir = resolveEngineDataDir(ID, { platform: 'darwin', env: {}, homedir: '/Users/dj', appDataDir: '/Users/dj/Library/Application Support' });
	assert.equal(dir, '/Users/dj/Library/Application Support/com.opendj.desktop');
});

test('linux keeps XDG_DATA_HOME, then ~/.local/share, byte for byte', () => {
	const withXdg = resolveEngineDataDir(ID, { platform: 'linux', env: { XDG_DATA_HOME: '/data/xdg' }, homedir: '/home/dj', appDataDir: '/home/dj/.config' });
	assert.equal(withXdg, '/data/xdg/com.opendj.desktop');
	const without = resolveEngineDataDir(ID, { platform: 'linux', env: {}, homedir: '/home/dj', appDataDir: '/home/dj/.config' });
	assert.equal(without, '/home/dj/.local/share/com.opendj.desktop');
});

const CHILD_SCRIPT = [
	'const m = require(process.argv[1]);',
	"const os = require('node:os');",
	"const path = require('node:path');",
	`process.stdout.write(m.resolveEngineDataDir('${ID}', { platform: process.platform, env: process.env, homedir: os.homedir(), appDataDir: path.join(os.homedir(), 'Library', 'Application Support') }));`
].join('\n');

test('a real child process on this OS resolves from its own environment', () => {
	const childLocal = win('C:', 'Users', 'child', 'AppData', 'Local');
	const env = { ...process.env, LOCALAPPDATA: process.platform === 'win32' ? childLocal : '/unused' };
	const ok = spawnSync(process.execPath, ['-e', CHILD_SCRIPT, MODULE_JS], { env, encoding: 'utf8' });
	assert.equal(ok.status, 0, ok.stderr);
	if (process.platform === 'win32') assert.equal(ok.stdout, win(childLocal, ID));
	else assert.ok(ok.stdout.endsWith(ID), ok.stdout);
	assert.ok(os.homedir() !== '');
});

test(
	'a real Windows child process with LOCALAPPDATA removed exits nonzero naming it',
	{ skip: process.platform !== 'win32' && 'POSIX_ONLY: the win32 arm is only reachable on Windows; the pure-function cases above cover it' },
	() => {
		const { LOCALAPPDATA: _dropped, ...without } = process.env;
		const missing = spawnSync(process.execPath, ['-e', CHILD_SCRIPT, MODULE_JS], { env: without, encoding: 'utf8' });
		assert.notEqual(missing.status, 0, 'child resolved a data dir with LOCALAPPDATA unset');
		assert.match(missing.stderr, /LOCALAPPDATA/);
	}
);
