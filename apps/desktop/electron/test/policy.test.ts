// Port of main.rs's tests plus the Electron-only origin policy (D4).
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';

import {
	STARTING_WINDOW_H_PT,
	STARTING_WINDOW_W_PT,
	USAGE,
	classifyMainFrameNavigation,
	PACKAGED_REMOTE_DEBUGGING_REFUSAL,
	argvRequestsRemoteDebugging,
	devCdpPortFromEnv,
	diagnosticOutput,
	isExternalUrlAllowed,
	packagedRemoteDebuggingRefusal,
	isTrustedPage,
	macAppSupportDir,
	DEVELOPER_MENU_ROLES,
	type MenuItemSpec,
	macMenuTemplate,
	parseDiagnosticFlag,
	permissionAllowed,
	readAudioEngineChoice,
	readBuildStamp,
	shellBuildIdentity,
	startingWindowSize,
	validateDataDirContainedInHome,
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

// ----- packaged CDP refusal (ELECTRON-6877, issue #6873) -------------------------
test('argv remote-debugging switches are recognized', () => {
	assert.equal(argvRequestsRemoteDebugging(['--remote-debugging-port=9222']), true);
	assert.equal(argvRequestsRemoteDebugging(['--remote-debugging-port', '9222']), true);
	assert.equal(argvRequestsRemoteDebugging(['--remote-debugging-address=127.0.0.1']), true);
	assert.equal(argvRequestsRemoteDebugging(['--remote-debugging-pipe']), true);
	assert.equal(argvRequestsRemoteDebugging(['--no-sandbox']), false);
});
test('packaged builds refuse remote debugging on argv or Chromium switches', () => {
	const refusal = packagedRemoteDebuggingRefusal(true, ['app', '--remote-debugging-port=9222'], false);
	assert.equal(refusal, PACKAGED_REMOTE_DEBUGGING_REFUSAL);
	assert.equal(packagedRemoteDebuggingRefusal(true, ['app'], true), PACKAGED_REMOTE_DEBUGGING_REFUSAL);
	assert.equal(packagedRemoteDebuggingRefusal(false, ['app', '--remote-debugging-port=9222'], false), null);
});
test('unpackaged dev builds accept OPENDJ_REMOTE_DEBUGGING_PORT only', () => {
	assert.equal(devCdpPortFromEnv(false, '9222'), '9222');
	assert.equal(devCdpPortFromEnv(false, '999999'), null);
	assert.equal(devCdpPortFromEnv(true, '9222'), null);
	assert.equal(devCdpPortFromEnv(false, undefined), null);
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
// External allow-list (ELECTRON-05, EL-17-F8): mirrors Tauri opener scope.
const fourDigitPort = 1000 + (process.pid % 9000);
const fiveDigitPort = 10000 + (process.pid % 55536);

test('openExternal allows named local http with four-digit port and slash', () => {
	const url = `http://opendj.localhost:${fourDigitPort}/settings`;
	assert.equal(isExternalUrlAllowed(url), true, url);
});
test('openExternal allows named local http with five-digit port and path', () => {
	const url = `http://opendj.localhost:${fiveDigitPort}/performance/deck`;
	assert.equal(isExternalUrlAllowed(url), true, url);
});
test('openExternal allows Google OAuth consent URL with query marker', () => {
	const url =
		'https://accounts.google.com/o/oauth2/v2/auth?client_id=example&redirect_uri=opendj&response_type=code';
	assert.equal(isExternalUrlAllowed(url), true, url);
});
test('openExternal allows exact BlackHole installer URL', () => {
	assert.equal(isExternalUrlAllowed('https://existential.audio/blackhole/'), true);
});
test('openExternal allows the https links the UI already renders (purchase, privacy)', () => {
	for (const url of [
		'https://www.beatport.com/track/example/123456',
		'https://artist.bandcamp.com/track/example',
		'https://open-dj.com/privacy',
		'https://existential.audio/blackhole/download'
	]) {
		assert.equal(isExternalUrlAllowed(url), true, url);
	}
});
test('openExternal rejects userinfo attacker shape on named local port', () => {
	const url = `http://opendj.localhost:${fourDigitPort}@attacker.example/`;
	assert.equal(isExternalUrlAllowed(url), false, url);
});
const USERINFO_AT = '@';
test('openExternal rejects named local and OAuth near-misses', () => {
	for (const [label, url] of [
		['no port', 'http://opendj.localhost/settings'],
		['three-digit port', `http://opendj.localhost:${100 + (process.pid % 900)}/`],
		['six-digit port', `http://opendj.localhost:${fiveDigitPort}0/`],
		['no slash after port', `http://opendj.localhost:${fourDigitPort}`],
		['lookalike host', `http://opendj.localhost.evil:${fourDigitPort}/`],
		['explicit userinfo', `http://user:pass@opendj.localhost:${fourDigitPort}/`],
		// userinfo built at runtime so the source never carries a mailbox-shaped string (oss_tip_audit)
		['google userinfo', `https://evil${USERINFO_AT}accounts.google.com/o/oauth2/v2/auth?x=1`],
		['plain http elsewhere', 'http://example.com/'],
		['file', 'file:///Applications/Calculator.app'],
		['smb', 'smb://host/share'],
		['javascript', 'javascript:alert(1)'],
		['malformed', 'not a url']
	] as const) {
		assert.equal(isExternalUrlAllowed(url), false, label);
	}
});

test('classifyMainFrameNavigation: trusted, external allow-list, and denied URLs', () => {
	const googleAuth = 'https://accounts.google.com/o/oauth2/v2/auth?client_id=x&redirect_uri=y';
	const namedLocal = `http://opendj.localhost:${fourDigitPort}/settings`;
	const cases: Array<[string, ReturnType<typeof classifyMainFrameNavigation>]> = [
		['opendj://app/index.html', 'allow'],
		[`http://127.0.0.1:${anyPort}/performance`, 'allow'],
		[`http://localhost:${anyPort}/`, 'allow'],
		[googleAuth, 'open-external'],
		[namedLocal, 'open-external'],
		['https://existential.audio/blackhole/', 'open-external'],
		['https://www.beatport.com/track/x/1', 'open-external'],
		['http://example.com/', 'deny'],
		['http://192.0.2.1/', 'deny'],
		['file:///etc/passwd', 'deny'],
		['data:text/html,hi', 'deny'],
		['smb://host/share', 'deny'],
		['not a url', 'deny']
	];
	for (const [url, expected] of cases) {
		assert.equal(classifyMainFrameNavigation(url), expected, url);
	}
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

// ----- data dir follows HOME and stays inside it (#6068) ---------------------------
// - if the macOS data dir is not read from HOME then a HOME-redirected acceptance
//   run opens the real library -> broken
// - if a data dir outside HOME, lexically or through a symlink, is accepted then
//   the run boots against the real library before HTTP exists -> broken
// - if the real user's path moves then the Electron and Tauri shells stop
//   sharing com.opendj.desktop -> broken
const isolatedHome = (): string => path.join(scratchDir('home-6068'), 'home');

test('the macOS data dir parent is HOME/Library/Application Support, as Tauri resolves it', () => {
	const home = path.join(path.sep, 'Users', 'dj');
	assert.equal(
		path.join(macAppSupportDir(home), 'com.opendj.desktop'),
		path.join(home, 'Library', 'Application Support', 'com.opendj.desktop')
	);
});

test('a data dir nested under HOME is accepted', () => {
	const home = isolatedHome();
	assert.doesNotThrow(() => validateDataDirContainedInHome(path.join(macAppSupportDir(home), 'com.opendj.desktop'), home));
});

test('a data dir outside HOME fails closed, including a sibling that shares its prefix', () => {
	const home = isolatedHome();
	assert.throws(() => validateDataDirContainedInHome(path.join(path.dirname(home), 'real-store'), home), /not inside HOME/);
	assert.throws(() => validateDataDirContainedInHome(`${home}2/Library`, home), /not inside HOME/);
});

test('a lexical escape under HOME fails closed', () => {
	const home = isolatedHome();
	assert.throws(() => validateDataDirContainedInHome(`${home}/../../escape/Library`, home), /parent \(\.\.\)/);
});

test('a symlink under HOME to an outside store fails closed, and a real folder passes', {
	skip: process.platform === 'win32'
		? 'UNAVAILABLE on Windows: fs.symlinkSync of a directory needs Developer Mode or SeCreateSymbolicLinkPrivilege, which CI runners lack'
		: false
}, () => {
	const root = scratchDir('home-6068-symlink');
	const home = path.join(root, 'home');
	const outside = path.join(root, 'real-library');
	fs.mkdirSync(path.join(home, 'Library'), { recursive: true });
	fs.mkdirSync(outside, { recursive: true });
	const link = path.join(home, 'Library', 'Application Support');
	fs.symlinkSync(outside, link);
	assert.throws(() => validateDataDirContainedInHome(path.join(link, 'com.opendj.desktop'), home), /resolves to/);
	assert.doesNotThrow(() => validateDataDirContainedInHome(path.join(home, 'Library', 'Real Support', 'com.opendj.desktop'), home));
});

test('a missing or relative HOME, or a relative data dir, fails closed', () => {
	const home = isolatedHome();
	const nested = path.join(home, 'Library');
	assert.throws(() => validateDataDirContainedInHome(nested, undefined), /HOME is unset/);
	assert.throws(() => validateDataDirContainedInHome(nested, ''), /HOME is unset/);
	assert.throws(() => validateDataDirContainedInHome('Library/App', home), /not an absolute path/);
	assert.throws(() => validateDataDirContainedInHome(nested, 'relative-home'), /HOME is unset/);
});

// ----- macOS application menu ------------------------------------------------------
// - if a packaged build has a Reload item then Cmd-R reloads the page mid-set and
//   stops the decks -> broken
// - if a packaged build has a developer tools item then the CDP refusal
//   (ELECTRON-6877) has a hole next to it -> broken
// - if the Edit or Window roles go then Cmd-C/V and Cmd-M stop working -> broken
function menuRoles(items: MenuItemSpec[]): string[] {
	return items.flatMap((item) => [...(item.role === undefined ? [] : [item.role]), ...menuRoles(item.submenu ?? [])]);
}

test('a packaged menu has no reload, developer tools or zoom item', () => {
	const roles = menuRoles(macMenuTemplate('Open DJ', true));
	assert.deepEqual(roles.filter((role) => DEVELOPER_MENU_ROLES.has(role)), []);
});

test('the menu keeps quit, close, the edit roles, full screen and the window menu', () => {
	const roles = menuRoles(macMenuTemplate('Open DJ', true));
	for (const role of ['quit', 'close', 'undo', 'redo', 'cut', 'copy', 'paste', 'selectAll', 'togglefullscreen', 'windowMenu']) {
		assert.ok(roles.includes(role), `missing ${role}`);
	}
	assert.equal(macMenuTemplate('Open DJ', true)[0]?.label, 'Open DJ');
});

test('an unpackaged dev build keeps reload and developer tools (the opposite direction)', () => {
	const roles = menuRoles(macMenuTemplate('Open DJ', false));
	assert.ok(roles.includes('reload') && roles.includes('toggleDevTools'));
});
