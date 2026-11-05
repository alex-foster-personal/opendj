import * as assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';

import { scratchDir } from './helpers';

const packageRoot = path.join(__dirname, '..', '..');
const pkg = JSON.parse(fs.readFileSync(path.join(packageRoot, 'package.json'), 'utf8')) as {
	scripts: { 'pack:dir': string; 'pack:dir:win': string };
	version: string;
	build: {
		appId: string;
		files: string[];
		extraResources: { from: string; to: string }[];
		electronFuses?: Record<string, boolean>;
		mac: {
			icon?: unknown;
			minimumSystemVersion?: string;
			category?: string;
			bundleShortVersion?: string;
			bundleVersion?: string;
		};
		win: { appId?: string; icon?: unknown; target: { target: string; arch: string[] }[] };
	};
};

/** electron-builder converts a PNG to .ico only from this edge up. */
const MIN_WIN_ICON_EDGE_PX = 256;

/** electron-builder refuses a mac icon below this edge, and would otherwise ship its stock one. */
const MIN_ICON_EDGE_PX = 512;
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

/** Width and height from a PNG's IHDR chunk; throws when the bytes are not a PNG. */
function pngSize(file: string): { width: number; height: number } {
	const bytes = fs.readFileSync(file);
	assert.ok(bytes.subarray(0, 8).equals(PNG_SIGNATURE), `${file} is not a PNG`);
	assert.equal(bytes.toString('latin1', 12, 16), 'IHDR', `${file} has no IHDR chunk`);
	return { width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20) };
}

test('pack:dir stamps build-identity before electron-builder', () => {
	assert.match(pkg.scripts['pack:dir'], /node scripts\/stamp-build\.mjs/);
	const stampAt = pkg.scripts['pack:dir'].indexOf('stamp-build');
	const builderAt = pkg.scripts['pack:dir'].indexOf('electron-builder');
	assert.ok(stampAt !== -1 && builderAt !== -1 && stampAt < builderAt);
});

test('pack:dir checks the packed payload against the stamp after electron-builder (EL-17-F12)', () => {
	const builderAt = pkg.scripts['pack:dir'].indexOf('electron-builder');
	const verifyAt = pkg.scripts['pack:dir'].indexOf('node scripts/verify-pack.mjs');
	assert.ok(builderAt !== -1 && verifyAt > builderAt, pkg.scripts['pack:dir']);
});

test('the packaged app ships the bootstrap setup directory', () => {
	assert.ok(pkg.build.extraResources.some((entry) => entry.from === '../setup' && entry.to === 'setup'));
});

test('pack:dir still ships the payload placeholder as extraResources', () => {
	assert.ok(pkg.build.extraResources.some((entry) => entry.from === 'payload' && entry.to === 'payload'));
});

test('the packaged bundle icon is the Open DJ mark, not the stock Electron one', () => {
	const icon = pkg.build.mac.icon;
	assert.ok(typeof icon === 'string' && icon !== '', 'build.mac.icon is missing, so the bundle would carry electron.icns');
	const file = path.join(packageRoot, icon);
	assert.ok(fs.existsSync(file), `build.mac.icon points at ${icon}, which does not exist`);
	const { width, height } = pngSize(file);
	assert.equal(width, height, `${icon} is ${width}x${height}, not square`);
	assert.ok(width >= MIN_ICON_EDGE_PX, `${icon} is ${width}px wide; electron-builder needs ${MIN_ICON_EDGE_PX}px or more`);
});

test('the packaged app ships the assets the runtime dock icon reads', () => {
	const icon = pkg.build.mac.icon as string;
	const dir = path.dirname(icon);
	assert.ok(pkg.build.files.includes(`${dir}/**/*`), `build.files does not pack ${dir}/, so main.ts cannot load ${icon}`);
});

test('pack:dir:win stamps build-identity, then builds an unsigned Windows dir', () => {
	const script = pkg.scripts['pack:dir:win'];
	assert.ok(script.indexOf('stamp-build') !== -1 && script.indexOf('stamp-build') < script.indexOf('electron-builder'));
	assert.match(script, /--dir --win --x64/);
	// Without a certificate electron-builder waits on signtool; signing is the owner's input.
	assert.match(script, /-c\.win\.signExecutable=false/);
});

test('pack:dir:win refuses an empty payload before electron-builder runs', () => {
	const script = pkg.scripts['pack:dir:win'];
	assert.ok(script.indexOf('require-payload') !== -1 && script.indexOf('require-payload') < script.indexOf('electron-builder'));
});

test('pack:dir:win uses no POSIX-only env prefix, because pnpm runs it through cmd.exe on Windows', () => {
	assert.doesNotMatch(pkg.scripts['pack:dir:win'], /(^|&&)\s*[A-Z_]+=\S+\s/);
});

test('the Windows build targets an x64 dir and carries the Open DJ icon, not the stock one', () => {
	assert.deepEqual(pkg.build.win.target, [{ target: 'dir', arch: ['x64'] }]);
	const icon = pkg.build.win.icon;
	assert.ok(typeof icon === 'string' && icon !== '', 'build.win.icon is missing, so Open DJ.exe would carry the Electron icon');
	const file = path.join(packageRoot, icon);
	assert.ok(fs.existsSync(file), `build.win.icon points at ${icon}, which does not exist`);
	const { width, height } = pngSize(file);
	assert.equal(width, height, `${icon} is ${width}x${height}, not square`);
	assert.ok(width >= MIN_WIN_ICON_EDGE_PX, `${icon} is ${width}px wide; electron-builder needs ${MIN_WIN_ICON_EDGE_PX}px or more`);
});

test('the Windows target has its own appId, distinct from the Tauri app and the mac identity', () => {
	const tauriIdentifier = (JSON.parse(fs.readFileSync(path.join(packageRoot, '..', 'src-tauri', 'tauri.conf.json'), 'utf8')) as { identifier: string }).identifier;
	assert.equal(pkg.build.appId, tauriIdentifier, 'the mac appId must stay the Tauri identifier (library identity)');
	const winAppId = pkg.build.win?.appId;
	assert.equal(winAppId, 'com.opendj.desktop.electron');
	assert.notEqual(winAppId, tauriIdentifier, 'Windows appId equals the Tauri identifier');
});

// ----- parity rows 63, 64: fuses and Info.plist ---------------------------------------
// The Tauri binary cannot be run as Node; an Electron one can unless these fuses are flipped.
test('if a Node entry-point fuse is left on or asar integrity is off then the packed app is not hardened (row 63)', () => {
	const fuses = pkg.build.electronFuses;
	assert.ok(fuses !== undefined, 'build.electronFuses is missing');
	assert.equal(fuses.runAsNode, false, 'ELECTRON_RUN_AS_NODE would turn the app into a Node runtime');
	assert.equal(fuses.enableNodeOptionsEnvironmentVariable, false, 'NODE_OPTIONS would inject code');
	assert.equal(fuses.enableNodeCliInspectArguments, false, '--inspect would open a debugger');
	assert.equal(fuses.enableEmbeddedAsarIntegrityValidation, true);
	assert.equal(fuses.onlyLoadAppFromAsar, true);
});

test('if fuses are flipped on an unsigned mac build without re-signing then arm64 kills the app at launch (row 63)', () => {
	// identity: null skips signing, and flipping a fuse breaks the stock ad-hoc signature.
	assert.equal(pkg.build.electronFuses?.resetAdHocDarwinSignature, true);
});

test('if cookie encryption is switched on then every unsigned local rebuild raises a Keychain prompt (row 63)', () => {
	// Safe Storage's Keychain item is bound to the code signature, which an ad-hoc rebuild changes. Not a Tauri feature either.
	assert.notEqual(pkg.build.electronFuses?.enableCookieEncryption, true);
});

test('if the mac minimum, category or numeric bundle versions are missing then Info.plist differs from Tauri (row 64)', () => {
	const mac = pkg.build.mac;
	assert.equal(mac.minimumSystemVersion, '13.0', 'ADR-NEW-electron-is-the-desktop-shell sets the macOS minimum at 13');
	assert.equal(mac.category, 'public.app-category.music', 'Tauri ships category Music');
	// electron-builder would otherwise write the semver string (1.0.0-alpha.1), which is not a valid CFBundleShortVersionString.
	const numericCore = pkg.version.split('-')[0];
	assert.match(numericCore ?? '', /^\d+\.\d+\.\d+$/);
	assert.equal(mac.bundleShortVersion, numericCore, `bump build.mac.bundleShortVersion with package.json version ${pkg.version}`);
	assert.equal(mac.bundleVersion, numericCore, `bump build.mac.bundleVersion with package.json version ${pkg.version}`);
});

test('if pack:dir does not read the fuses back from the packed Electron Framework then a config that never applied passes', () => {
	const script = (pkg.scripts as Record<string, string>)['pack:dir'] ?? '';
	const builderAt = script.indexOf('electron-builder');
	const readAt = script.indexOf('node scripts/read-fuses.mjs');
	assert.ok(builderAt !== -1 && readAt > builderAt, script);
	assert.match(script, /Electron Framework\.framework\/Electron Framework/);
});

const SENTINEL = 'dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX';
/** A binary-like buffer: filler, the sentinel, wire version 1, then one state byte per fuse ('0' off, '1' on). */
function fakeFuseBinary(states: string): Buffer {
	return Buffer.concat([Buffer.from('filler'), Buffer.from(SENTINEL), Buffer.from([1, states.length]), Buffer.from(states, 'latin1'), Buffer.from('tail')]);
}

test('if read-fuses meets a hardened wire it passes, and if runAsNode is on it fails naming it (non-fuse options ignored)', () => {
	const verifier = path.join(packageRoot, 'scripts', 'read-fuses.mjs');
	const binary = path.join(scratchDir('mac-fuses'), 'Electron Framework');
	// Wire order: runAsNode, cookie, nodeOptions, inspect, asarIntegrity, onlyAsar, v8snapshot, fileProtocol, wasmTrap.
	fs.writeFileSync(binary, fakeFuseBinary('000011001'));
	const ok = spawnSync(process.execPath, [verifier, binary], { encoding: 'utf8' });
	assert.equal(ok.status, 0, ok.stderr);
	fs.writeFileSync(binary, fakeFuseBinary('100011001'));
	const bad = spawnSync(process.execPath, [verifier, binary], { encoding: 'utf8' });
	assert.notEqual(bad.status, 0);
	assert.match(bad.stderr, /runAsNode is enabled, expected disabled/);
	assert.doesNotMatch(bad.stderr, /resetAdHocDarwinSignature/);
});
