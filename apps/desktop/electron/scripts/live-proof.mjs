// Proves the already-open Electron window owns its engine and shows a library.
// Start the shell with OPENDJ_REMOTE_DEBUGGING_PORT set (unpackaged only),
// and OPENDJ_SHELL_DATA_DIR set to the data dir the shell supervises.
// An attached OPENDJ_ENGINE_ORIGIN window has no .engine.shell.json and fails.

import * as fs from 'node:fs';
import * as path from 'node:path';

import { chromium } from '@playwright/test';

const PROBE_MS = 8_000;
const port = process.env.OPENDJ_REMOTE_DEBUGGING_PORT ?? '9222';
const browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
const pages = browser.contexts().flatMap((context) => context.pages());

async function pageIdentity(candidate) {
	return candidate.evaluate(async (timeoutMs) => {
		const scope = globalThis;
		const build = scope.OPENDJ_SHELL_BUILD;
		const bridge = scope.opendjShell;
		const response = await fetch('/api/v1/health', { signal: AbortSignal.timeout(timeoutMs) });
		if (!response.ok) throw new Error(`health ${response.status}`);
		return {
			shell: build && typeof build === 'object' ? build.shell ?? null : null,
			kind: bridge && typeof bridge === 'object' ? bridge.kind ?? null : null,
			health: await response.json()
		};
	}, PROBE_MS);
}

let page;
let identity;
const rejected = [];
for (const candidate of pages) {
	if (!candidate.url().includes('/performance')) continue;
	const found = await pageIdentity(candidate);
	if (found.shell === 'electron' && found.kind === 'electron') {
		page = candidate;
		identity = found;
		break;
	}
	rejected.push(`${candidate.url()} shell=${found.shell} kind=${found.kind}`);
}
if (page === undefined || identity === undefined) {
	const urls = pages.map((candidate) => candidate.url());
	throw new Error(
		`no Electron /performance page on CDP :${port}; open: ${urls.join(', ') || '(none)'}; rejected: ${rejected.join(' | ') || '(none)'}`
	);
}
const health = identity.health;
const tracks = health?.state_db?.tracks;
const dbPath = health?.state_db?.path;
if (typeof tracks !== 'number' || tracks < 1) {
	throw new Error(`performance page library is empty: ${JSON.stringify(health?.state_db)}`);
}
const dataDir = process.env.OPENDJ_SHELL_DATA_DIR;
if (dataDir === undefined || dataDir.trim() === '') {
	throw new Error('OPENDJ_SHELL_DATA_DIR is required so the proof can see the shell the window owns');
}
const shellJsonPath = path.join(dataDir, '.engine.shell.json');
const shellJson = JSON.parse(fs.readFileSync(shellJsonPath, 'utf8'));
if (typeof shellJson.health_port !== 'number') {
	throw new Error(`${shellJsonPath} has no health_port; the shell is not supervising an engine`);
}
const shellHealth = await fetch(`http://127.0.0.1:${shellJson.health_port}/api/v1/health`, {
	signal: AbortSignal.timeout(PROBE_MS)
}).then(async (response) => {
	if (!response.ok) throw new Error(`shell health ${response.status}`);
	return response.json();
});
if (shellHealth.engine !== 'running' || typeof shellHealth.lock_pid !== 'number') {
	throw new Error(`shell is not running its engine: ${JSON.stringify(shellHealth)}`);
}
const pagePort = new URL(page.url()).port;
const lock = JSON.parse(fs.readFileSync(path.join(dataDir, '.engine.lock'), 'utf8'));
if (String(lock.port) !== pagePort) {
	throw new Error(`performance page port ${pagePort} is not the shell lock port ${String(lock.port)}`);
}
console.log(
	`live-proof ok ${page.url()} tracks=${tracks} db=${dbPath} shell_pid=${shellHealth.lock_pid} engine_port=${lock.port}`
);
await browser.close();
