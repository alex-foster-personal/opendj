import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { appVersionFromAppPackage, APP_PACKAGE_REL } from '../../vite-app-version.ts';

// N12, public-launch sweep (Wed 7 Oct 2026): the public 1.0.0-alpha.1 build
// reported 0.1.0 from the engine, Settings and the usage heartbeat. Every
// surface now reads apps/desktop/electron/package.json `version`; these tests
// hold the mirrors.
const REPO = fileURLToPath(new URL('../../../../../', import.meta.url));
const APP_VERSION = JSON.parse(readFileSync(join(REPO, APP_PACKAGE_REL), 'utf8')).version;

test('the bundle reads its version from the app package.json', () => {
	assert.equal(typeof APP_VERSION, 'string');
	assert.equal(appVersionFromAppPackage(REPO), APP_VERSION);
});

test('an app package.json without a version fails the build instead of guessing', () => {
	const root = mkdtempSync(join(tmpdir(), 'app-version-'));
	mkdirSync(join(root, 'apps/desktop/electron'), { recursive: true });
	writeFileSync(join(root, APP_PACKAGE_REL), JSON.stringify({ productName: 'x' }));
	assert.throws(() => appVersionFromAppPackage(root), /no non-empty string "version"/);
});

test('control: another version in the app package.json is what the bundle reports', () => {
	const root = mkdtempSync(join(tmpdir(), 'app-version-'));
	mkdirSync(join(root, 'apps/desktop/electron'), { recursive: true });
	writeFileSync(join(root, APP_PACKAGE_REL), JSON.stringify({ version: '9.9.9-beta.3' }));
	assert.equal(appVersionFromAppPackage(root), '9.9.9-beta.3');
});

test('the Tauri shell still reports the same app version while it ships', () => {
	const tauri = JSON.parse(readFileSync(join(REPO, 'apps/desktop/src-tauri/tauri.conf.json'), 'utf8'));
	assert.equal(
		tauri.version,
		APP_VERSION,
		'bump apps/desktop/src-tauri/tauri.conf.json "version" together with apps/desktop/electron/package.json'
	);
});
