/**
 * Wrap a Playwright `webServer.command` so the server dies with the run.
 *
 * Measured Mon 28 Sep 2026 against Playwright 1.61: a webServer SURVIVES when
 * `playwright test` gets SIGTERM or SIGKILL (only SIGINT runs its cleanup), and
 * is reparented to init/launchd with its port. An agent's command timeout, a
 * cancelled CI step and an OOM kill all deliver exactly those signals, so every
 * such run leaked its engine and vite servers.
 *
 * `scripts/server_owner_guard.py` runs the command inside the webServer's own
 * process group (so Playwright's normal teardown, which signals that group,
 * still reaches it), watches THIS runner process, and kills the whole group
 * when the runner disappears. It also stamps the server with
 * `AF_SERVICE_ID=<namespace>.test.<name>` so `scripts/orphan_reaper.py` can
 * attribute anything that still leaks.
 *
 * Regression lines:
 *   - if a guarded webServer is alive 5 s after the runner is SIGKILLed then broken
 *   - if a guarded webServer is alive 5 s after the runner is SIGTERMed then broken
 *   - if the repo venv python is missing and the config still loads then broken
 */
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../../../', import.meta.url));
const GUARD = join(REPOSITORY_ROOT, 'scripts', 'server_owner_guard.py');
const VENV_PYTHON = join(REPOSITORY_ROOT, '.venv', 'bin', 'python');

function shellArgument(value: string): string {
	return `'${value.replaceAll("'", "'\"'\"'")}'`;
}

/** `command`, run under the test-process guard owned by this Playwright runner. */
export function guardedWebServerCommand(name: string, command: string): string {
	if (!existsSync(VENV_PYTHON)) {
		throw new Error(
			`guardedWebServerCommand: ${VENV_PYTHON} is missing; run \`uv sync --extra dev\` at ${REPOSITORY_ROOT}`
		);
	}
	// `exec` so the guard REPLACES Playwright's detached shell and leads the
	// webServer's process group; the guard refuses to start otherwise.
	return `exec ${[
		VENV_PYTHON,
		GUARD,
		'--owner-pid',
		String(process.pid),
		'--name',
		name,
		'--',
		'/bin/sh',
		'-c',
		command
	]
		.map(shellArgument)
		.join(' ')}`;
}
