import { readFileSync } from 'node:fs';
import { join } from 'node:path';

/**
 * The app version the bundle reports, read at build time from the one source
 * every Open DJ surface uses: `apps/desktop/src-tauri/tauri.conf.json`
 * `version` (the launch-name ADR; the Python side is
 * `apps/shared/app_version.py`). vite.config.ts defines it as
 * `import.meta.env.VITE_APP_VERSION`, so a release bump is one edit there,
 * never a second literal here to forget (public-launch sweep N12).
 *
 * Throws when the config is missing or has no version: a build that cannot
 * name its version must fail, not ship a guess.
 */
export const TAURI_CONF_REL = 'apps/desktop/src-tauri/tauri.conf.json';

export function appVersionFromTauriConf(repositoryRoot: string): string {
	const path = join(repositoryRoot, TAURI_CONF_REL);
	const conf: unknown = JSON.parse(readFileSync(path, 'utf8'));
	const version =
		typeof conf === 'object' && conf !== null ? (conf as { version?: unknown }).version : undefined;
	if (typeof version !== 'string' || version.trim() === '') {
		throw new Error(`${path} has no non-empty string "version"; the bundle cannot name its app version`);
	}
	return version.trim();
}
