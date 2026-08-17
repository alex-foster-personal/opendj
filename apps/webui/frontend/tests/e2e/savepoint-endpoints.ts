/**
 * Shared endpoint + data-dir resolution for the savepoint smoke gate.
 *
 * Requirements:
 *
 * - ✔︎ Loopback origins only, with an explicit port, so a smoke run can never
 *   bind or talk to anything but this machine.
 * - ✔︎ The real library data dir is derived from the Git COMMON dir, so a
 *   worktree reads the primary checkout's ``data/`` with no hardcoded user path.
 * - ✔︎ Missing or malformed values fail at config load, never mid-test.
 *
 * Acceptance tests:
 *
 * - [if] an origin carries a path, no port, or a non-loopback host [then ⛔️]
 *   the config loads.
 * - [if] the resolved data dir has no ``state/state.db`` [then ⛔️] the run starts.
 * - [if] ``SAVEPOINT_SMOKE_DATA_DIR`` is set [then] it wins over the Git-derived dir.
 */
import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { isAbsolute, join, resolve } from 'node:path';

import { parseWebuiDevConfigPayload, type WebuiDevConfig } from '../../webui-port-config';

/**
 * This worktree's reserved backend/frontend pair, claimed but NOT availability
 * checked.
 *
 * Playwright re-evaluates the config inside every worker process, by which time
 * this suite's own servers hold the reserved ports - a `check` there would fail
 * the run against itself. Availability is enforced where it belongs instead:
 * `just savepoint-smoke` runs `webui-ports-check` before launching, Vite uses
 * `strictPort`, and uvicorn refuses to share a bound port.
 */
export function claimWebuiPorts(repositoryRoot: string): WebuiDevConfig {
	const payload = execFileSync(
		'uv',
		['run', '--no-sync', 'python', '-m', 'apps.webui.port_config', 'claim', '--json'],
		{ cwd: repositoryRoot, encoding: 'utf8', stdio: ['ignore', 'pipe', 'inherit'] }
	);
	return parseWebuiDevConfigPayload(payload);
}

export function requireLoopbackOrigin(name: string, raw: string | undefined): URL {
	if (raw === undefined || raw.trim() === '') {
		throw new Error(`${name} is required`);
	}
	const url = new URL(raw.trim());
	if (url.protocol !== 'http:') {
		throw new Error(`${name} must use http, got ${url.protocol}`);
	}
	if (url.hostname !== '127.0.0.1' && url.hostname !== 'localhost') {
		throw new Error(`${name} must be loopback, got ${url.hostname}`);
	}
	if (url.port === '') {
		throw new Error(`${name} must include an explicit port`);
	}
	if (url.pathname !== '/' || url.search !== '' || url.hash !== '') {
		throw new Error(`${name} must be an origin without path, query, or fragment`);
	}
	return url;
}

/**
 * Absolute path of the checkout that owns the shared ``.git`` directory.
 *
 * In a linked worktree the common dir is ``<primary>/.git``; in the primary
 * checkout it is ``<primary>/.git`` as well, so both resolve to the same root.
 */
function _primaryCheckoutRoot(repositoryRoot: string): string {
	const commonDir = execFileSync(
		'git',
		['rev-parse', '--path-format=absolute', '--git-common-dir'],
		{ cwd: repositoryRoot, encoding: 'utf8' }
	).trim();
	if (commonDir === '' || !isAbsolute(commonDir)) {
		throw new Error(`git --git-common-dir did not return an absolute path, got ${commonDir}`);
	}
	return resolve(commonDir, '..');
}

/**
 * The real library ``data/`` dir the smoke backend reads through ``MDT_DATA_DIR``.
 *
 * A linked worktree ships an empty ``data/``, so the primary checkout's dir is
 * the only place the real rekordbox + state databases live. The smoke issues no
 * library-mutating request (asserted in the spec), so this stays a read path.
 */
export function requireRealLibraryDataDir(repositoryRoot: string): string {
	const override = process.env.SAVEPOINT_SMOKE_DATA_DIR;
	const dataDir =
		override !== undefined && override.trim() !== ''
			? resolve(override.trim())
			: join(_primaryCheckoutRoot(repositoryRoot), 'data');
	const stateDb = join(dataDir, 'state', 'state.db');
	if (!existsSync(stateDb)) {
		throw new Error(
			`savepoint smoke needs the real library: ${stateDb} does not exist. ` +
				'Point SAVEPOINT_SMOKE_DATA_DIR at a checkout whose data/ holds state/state.db.'
		);
	}
	const masterPlainDb = join(dataDir, 'master.plain.db');
	if (!existsSync(masterPlainDb)) {
		throw new Error(
			`savepoint smoke needs the decrypted rekordbox db: ${masterPlainDb} does not exist.`
		);
	}
	return dataDir;
}
