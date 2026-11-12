/**
 * MIDI load failure reporting wired from app-init without importing apply.ts.
 * apply.ts calls the reporter when midi-ui-state fails to load; app-init sets
 * the toast sink at boot. Kept tiny so first paint does not pull the settings
 * allowlist module graph.
 */

const _logMidiLoadFailure = (exc: unknown): void =>
	console.error('[midi] MIDI could not load', exc);
let _reportMidiLoadFailure = _logMidiLoadFailure;

/** null restores the log-only default (app-init's teardown). */
export function setMidiLoadFailureReporter(report: ((exc: unknown) => void) | null): void {
	_reportMidiLoadFailure = report ?? _logMidiLoadFailure;
}

export function reportMidiLoadFailure(exc: unknown): void {
	_reportMidiLoadFailure(exc);
}
