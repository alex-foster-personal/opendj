import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { appVersionFromTauriConf, TAURI_CONF_REL } from '../../vite-app-version.ts';

// N12, public-launch sweep (Wed 7 Oct 2026): the public 1.0.0-alpha.1 build
// reported 0.1.0 from the engine, Settings and the usage heartbeat. Every
// surface now reads tauri.conf.json `version`; these tests hold the mirrors.
const REPO = fileURLToPath(new URL('../../../../../', import.meta.url));
const TAURI_VERSION = JSON.parse(readFileSync(join(REPO, TAURI_CONF_REL), 'utf8')).version;

test('the bundle reads its version from tauri.conf.json', () => {
	assert.equal(typeof TAURI_VERSION, 'string');
	assert.equal(appVersionFromTauriConf(REPO), TAURI_VERSION);
});

test('a tauri.conf.json without a version fails the build instead of guessing', () => {
	const root = mkdtempSync(join(tmpdir(), 'app-version-'));
	mkdirSync(join(root, 'apps/desktop/src-tauri'), { recursive: true });
	writeFileSync(join(root, TAURI_CONF_REL), JSON.stringify({ productName: 'x' }));
	assert.throws(() => appVersionFromTauriConf(root), /no non-empty string "version"/);
});

test('control: another version in tauri.conf.json is what the bundle reports', () => {
	const root = mkdtempSync(join(tmpdir(), 'app-version-'));
	mkdirSync(join(root, 'apps/desktop/src-tauri'), { recursive: true });
	writeFileSync(join(root, TAURI_CONF_REL), JSON.stringify({ version: '9.9.9-beta.3' }));
	assert.equal(appVersionFromTauriConf(root), '9.9.9-beta.3');
});

test('the Electron shell reports the same app version (app.getVersion reads its package.json)', () => {
	const electron = JSON.parse(readFileSync(join(REPO, 'apps/desktop/electron/package.json'), 'utf8'));
	assert.equal(
		electron.version,
		TAURI_VERSION,
		'bump apps/desktop/electron/package.json "version" together with tauri.conf.json'
	);
});
