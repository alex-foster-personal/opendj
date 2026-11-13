/**
 * #12781: provision the Chromium the browser-backed unit tests launch.
 * stem-row.test.mjs and command-entry.test.mjs drive real Chromium and fail
 * loudly when it is missing; the frontend unit CI job never installed it, so
 * the gate leaned on a browser cache another job happened to fill. Under CI
 * this installs the Chromium matching the locked Playwright when absent
 * (no --with-deps: CI hosts carry the system libraries from provisioning).
 * Locally it does nothing, so a missing browser still fails the test itself.
 */
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

import { chromium } from 'playwright';

/** Return true when the CI environment variable names a CI run. */
export function isCi(env = process.env) {
	const value = (env.CI ?? '').trim().toLowerCase();
	return value !== '' && value !== '0' && value !== 'false';
}

/** Install Chromium under CI when the locked Playwright cannot find it; return the exit code. */
export function ensureUnitBrowsers({
	env = process.env,
	executablePath = () => chromium.executablePath(),
	exists = existsSync,
	run = (cmd, args) => spawnSync(cmd, args, { stdio: 'inherit' }).status ?? 1
} = {}) {
	if (!isCi(env)) return 0;
	if (exists(executablePath())) return 0;
	console.log(
		'[ensure-unit-browsers] Chromium missing under CI; running playwright install chromium'
	);
	return run('pnpm', ['exec', 'playwright', 'install', 'chromium']);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
	process.exit(ensureUnitBrowsers());
}
