// Port of main.rs's tests plus the Electron-only origin policy (D4).
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';

import {
	STARTING_WINDOW_H_PT,
	STARTING_WINDOW_W_PT,
	USAGE,
	diagnosticOutput,
	isExternalUrlAllowed,
	isTrustedPage,
	parseDiagnosticFlag,
	permissionAllowed,
	readAudioEngineChoice,
	readBuildStamp,
	shellBuildIdentity,
	startingWindowSize,
	writeAudioEngineChoice
} from '../src/policy';
import { scratchDir } from './helpers';

// ----- window size --------------------------------------------------------------
const ROOMY = { width: 2560, height: 1440 };
const LAPTOP = { width: 1512, height: 916 };

// Any loopback port: the shell never pins one (a literal here would trip
// tests/webui/test_reserved_fixed_ports_drift.py).
const anyPort = 40000 + (process.pid % 20000);

test('a monitor smaller than the target shrinks both dimensions', () => {
	assert.deepEqual(startingWindowSize(LAPTOP), LAPTOP);
});
test('only the dimension that overflows is clamped', () => {
	assert.deepEqual(startingWindowSize(ROOMY), { width: STARTING_WINDOW_W_PT, height: ROOMY.height });
});
test('a monitor big enough leaves the target alone', () => {
	assert.deepEqual(startingWindowSize({ width: 4000, height: 3000 }), { width: STARTING_WINDOW_W_PT, height: STARTING_WINDOW_H_PT });
});
test('an unresolvable or nonsense monitor applies the configured size unchanged', () => {
	const target = { width: STARTING_WINDOW_W_PT, height: STARTING_WINDOW_H_PT };
	assert.deepEqual(startingWindowSize(null), target);
	assert.deepEqual(startingWindowSize({ width: 0, height: 900 }), target);
	assert.deepEqual(startingWindowSize({ width: Number.NaN, height: 900 }), target);
});

// ----- diagnostic flags (INSTALL-24) --------------------------------------------
test('no args launches the app', () => assert.equal(parseDiagnosticFlag([]), null));
test('-h and --help are help', () => {
	assert.equal(parseDiagnosticFlag(['-h']), 'help');
	assert.equal(parseDiagnosticFlag(['--help']), 'help');
});
test('--version is version', () => assert.equal(parseDiagnosticFlag(['--version']), 'version'));
test('help is found even when not the first argument', () => assert.equal(parseDiagnosticFlag(['--no-sandbox', '--help']), 'help'));
test('an unrelated argument launches the app', () => assert.equal(parseDiagnosticFlag(['--engine-origin-probe']), null));
test('diagnostic output is usage or the version line', () => {
	assert.equal(diagnosticOutput(['--help'], '0.1.5'), USAGE);
	assert.equal(diagnosticOutput(['--version'], '0.1.5'), 'opendj-desktop 0.1.5\n');
	assert.equal(diagnosticOutput([], '0.1.5'), null);
});

// ----- origins -------------------------------------------------------------------
test('loopback http and the bundled page are trusted', () => {
	assert.equal(isTrustedPage(`http://127.0.0.1:${anyPort}/performance`), true);
	assert.equal(isTrustedPage(`http://localhost:${anyPort}/`), true);
	assert.equal(isTrustedPage('opendj://app/index.html?fatal=1'), true);
});
test('anything else is not: other hosts, https, file, lookalikes', () => {
	for (const url of [
		`https://127.0.0.1:${anyPort}/`,
		'http://127.0.0.1.evil.example/',
		`http://192.168.1.2:${anyPort}/`,
		'file:///etc/passwd',
		'opendj://evil/index.html',
		// EL-17-F13: Tauri's capabilities grant only 127.0.0.1 and localhost, never IPv6 loopback.
		`http://[::1]:${anyPort}/`,
		'data:text/html,hi',
		'about:blank',
		'not a url'
	]) {
		assert.equal(isTrustedPage(url), false, url);
	}
});
test('permissions: only the short list, only for loopback pages', () => {
	assert.equal(permissionAllowed('midi', `http://127.0.0.1:${anyPort}`), true);
	// EL-17-F14: Tauri's MIDI command sends three bounded bytes; no SysEx under either shell.
	assert.equal(permissionAllowed('midiSysex', `http://127.0.0.1:${anyPort}/`), false);
	assert.equal(permissionAllowed('speaker-selection', `http://localhost:${anyPort}`), true);
	assert.equal(permissionAllowed('geolocation', `http://127.0.0.1:${anyPort}`), false);
	assert.equal(permissionAllowed('notifications', `http://127.0.0.1:${anyPort}`), false);
	assert.equal(permissionAllowed('midi', 'https://example.com'), false);
	assert.equal(permissionAllowed('midi', 'opendj://app'), false);
});
test('openExternal takes web URLs only', () => {
	assert.equal(isExternalUrlAllowed('https://accounts.google.com/o/oauth2/v2/auth?x=1'), true);
	assert.equal(isExternalUrlAllowed('file:///Applications/Calculator.app'), false);
	assert.equal(isExternalUrlAllowed('smb://host/share'), false);
	assert.equal(isExternalUrlAllowed('javascript:alert(1)'), false);
});

// ----- build identity -------------------------------------------------------------
test('an unstamped build says so instead of inventing a sha', () => {
	const identity = shellBuildIdentity('0.1.5', {});
	assert.equal(identity.stamped, false);
	assert.equal(identity.git_sha, null);
	assert.equal(identity.git_dirty, null);
	assert.equal(identity.shell, 'electron');
});
test('a stamped build reads back its stamp', () => {
	const file = path.join(scratchDir('stamp'), 'build-identity.json');
	fs.writeFileSync(file, JSON.stringify({ git_sha: 'abc1234', git_dirty: '1', release_channel: 'beta', junk: 'x' }));
	const identity = shellBuildIdentity('0.1.5', readBuildStamp(file));
	assert.equal(identity.stamped, true);
	assert.equal(identity.git_sha, 'abc1234');
	assert.equal(identity.git_dirty, true);
	assert.equal(identity.release_channel, 'beta');
	assert.equal('junk' in identity, false);
});
test('a missing or corrupt stamp file is unstamped, not a crash', () => {
	assert.deepEqual(readBuildStamp('/nonexistent/build-identity.json'), {});
	const file = path.join(scratchDir('stamp-bad'), 'build-identity.json');
	fs.writeFileSync(file, '{');
	assert.deepEqual(readBuildStamp(file), {});
});

// ----- audio engine choice (NAE-8118) -------------------------------------------------
test('the audio engine choice round-trips through its file and a missing file is no choice', () => {
	const file = path.join(scratchDir('engine-choice'), 'nested', 'audio-engine-choice.json');
	assert.equal(readAudioEngineChoice(file), null);
	assert.equal(writeAudioEngineChoice(file, 'webaudio'), 'webaudio');
	assert.equal(readAudioEngineChoice(file), 'webaudio');
	writeAudioEngineChoice(file, 'rust');
	assert.equal(readAudioEngineChoice(file), 'rust');
	assert.deepEqual(fs.readdirSync(path.dirname(file)), ['audio-engine-choice.json']);
});

test('an unknown audio engine choice is refused on write and on read, never defaulted', () => {
	const file = path.join(scratchDir('engine-choice-bad'), 'audio-engine-choice.json');
	writeAudioEngineChoice(file, 'rust');
	assert.throws(() => writeAudioEngineChoice(file, 'tauri'), /must be webaudio\|rust/);
	assert.equal(readAudioEngineChoice(file), 'rust');
	fs.writeFileSync(file, JSON.stringify({ choice: 'tauri' }));
	assert.throws(() => readAudioEngineChoice(file), /is not one of webaudio, rust/);
	fs.writeFileSync(file, 'not json');
	assert.throws(() => readAudioEngineChoice(file));
});
