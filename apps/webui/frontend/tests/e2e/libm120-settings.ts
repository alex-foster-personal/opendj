/**
 * One source for the LIBM-120 bench's size, port and fixture directory.
 *
 * Both `playwright.libm120.config.ts` (which seeds and serves the engine) and
 * `libm120-scale-bench.spec.ts` (which measures it) read these values, so the
 * spec can never address a different engine port or fixture than the config
 * started (PR #4547 review: a 10k run once defaulted to two different ports).
 *
 * - [if] LIBM120_TRACKS is not 10000 or 20000 [then] import throws.
 * - [if] LIBM120_PORT is not a usable TCP port [then] import throws.
 * - [if] the platform is Windows [then] LIBM120_PLATFORM_SUPPORTED is false, the
 *   config starts no webServer, and the spec skips with LIBM120_POSIX_ONLY_REASON.
 */
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const LIBM120_PROJECT = 'libm120-chromium';

/**
 * The bench launches its engine through `guardedWebServerCommand`, the
 * launcher every Playwright config in this repo shares. That helper is POSIX
 * only, and no portable arm of it exists yet, so this bench names the
 * platform-specific part and skips on Windows (AGENTS.md "Portable by
 * default": a skip names what is platform-specific, never just the platform).
 */
export const LIBM120_PLATFORM_SUPPORTED = process.platform !== 'win32';
export const LIBM120_POSIX_ONLY_REASON =
	'LIBM-120 starts its engine through guardedWebServerCommand ' +
	'(tests/e2e/support/guarded-web-server.ts), which runs .venv/bin/python, chains the ' +
	'fixture build and the engine with /bin/sh -c, and relies on ' +
	"scripts/server_owner_guard.py's os.killpg process-group teardown; that launcher has " +
	'no Windows arm yet';
export const LIBM120_SEED = 2328;

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

export const LIBM120_TRACKS = Number(process.env.LIBM120_TRACKS ?? '20000');
export const LIBM120_PORT = Number(
	process.env.LIBM120_PORT ?? (LIBM120_TRACKS === 10_000 ? '8720' : '8721')
);
export const LIBM120_DATA_DIR = join(
	FRONTEND_ROOT,
	'tests',
	'e2e',
	'fixtures',
	`libm120-${LIBM120_TRACKS}`
);
export const LIBM120_MANIFEST = join(LIBM120_DATA_DIR, 'library-scale-fixture.json');

if (![10_000, 20_000].includes(LIBM120_TRACKS)) {
	throw new Error(`LIBM120_TRACKS must be 10000 or 20000, got ${LIBM120_TRACKS}`);
}
if (!Number.isInteger(LIBM120_PORT) || LIBM120_PORT < 1024 || LIBM120_PORT > 65535) {
	throw new Error(`LIBM120_PORT must be a valid TCP port, got ${LIBM120_PORT}`);
}
