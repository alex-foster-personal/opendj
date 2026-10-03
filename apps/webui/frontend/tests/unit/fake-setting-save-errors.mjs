/**
 * Test stand-in for `$lib/settings/setting-save-errors`: records every
 * reported settings save failure on `globalThis.__recordedSettingSaveErrors`.
 * Aliased in through load-typescript.mjs's `alias` option.
 */
export function installSettingSaveErrorSink() {}

export function reportSettingSaveError(message, cause) {
	if (!Array.isArray(globalThis.__recordedSettingSaveErrors)) {
		globalThis.__recordedSettingSaveErrors = [];
	}
	globalThis.__recordedSettingSaveErrors.push({ message, cause });
}
