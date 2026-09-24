// Pure shell policy: diagnostic flags, window size, build identity, which
// origins may use the native bridge, and what gets injected into each page.
// Everything here is free of Electron so node:test covers it directly.

import * as fs from 'node:fs';

// ----- diagnostic flags (INSTALL-24, issue #2868) ---------------------------
export const USAGE = `usage: opendj-desktop [-h] [--version]

Open DJ desktop shell -- boots the bundled engine and opens the app window.
Run with no arguments to launch normally.

options:
  -h, --help     show this help message and exit
  --version      show the shell's version and exit
`;

export type DiagnosticFlag = 'help' | 'version';

/** The first diagnostic flag anywhere in argv, or null to launch. */
export function parseDiagnosticFlag(args: readonly string[]): DiagnosticFlag | null {
	for (const arg of args) {
		if (arg === '-h' || arg === '--help') return 'help';
		if (arg === '--version') return 'version';
	}
	return null;
}

/** Text to print and exit 0 for a diagnostic flag, or null to launch. */
export function diagnosticOutput(args: readonly string[], appVersion: string): string | null {
	const flag = parseDiagnosticFlag(args);
	if (flag === 'help') return USAGE;
	if (flag === 'version') return `opendj-desktop ${appVersion}\n`;
	return null;
}

// ----- window size ------------------------------------------------------------
/** Logical points (CSS px), sized for decks, waveforms and browser side by side. */
export const STARTING_WINDOW_W_PT = 2035;
export const STARTING_WINDOW_H_PT = 1449;

/**
 * The configured size clamped per dimension to the monitor's work area (not
 * aspect-preserving). Electron's `workAreaSize` is already in DIPs, so no scale
 * divide is needed here, unlike Tauri's physical-pixel monitor geometry. A
 * non-finite or non-positive area is treated as unknown: no clamp.
 */
export function startingWindowSize(usable: { width: number; height: number } | null): { width: number; height: number } {
	const valid =
		usable !== null &&
		Number.isFinite(usable.width) &&
		Number.isFinite(usable.height) &&
		usable.width > 0 &&
		usable.height > 0;
	if (!valid) return { width: STARTING_WINDOW_W_PT, height: STARTING_WINDOW_H_PT };
	return {
		width: Math.floor(Math.min(STARTING_WINDOW_W_PT, usable.width)),
		height: Math.floor(Math.min(STARTING_WINDOW_H_PT, usable.height))
	};
}

// ----- build identity -----------------------------------------------------------
export interface BuildStamp {
	git_sha?: string;
	git_sha_full?: string;
	git_branch?: string;
	git_dirty?: string;
	built_at_utc?: string;
	lane_label?: string;
	release_channel?: string;
	evidence_written_at_utc?: string;
}

/**
 * Stamp env names, as the dmg recipe exports them for the Tauri build.
 * `scripts/stamp-build.js` bakes them into build-identity.json at package time.
 */
export const STAMP_ENV: Record<keyof BuildStamp, string> = {
	git_sha: 'OPENDJ_BUILD_GIT_SHA',
	git_sha_full: 'OPENDJ_BUILD_GIT_SHA_FULL',
	git_branch: 'OPENDJ_BUILD_GIT_BRANCH',
	git_dirty: 'OPENDJ_BUILD_GIT_DIRTY',
	built_at_utc: 'OPENDJ_BUILD_AT_UTC',
	lane_label: 'OPENDJ_BUILD_LANE_LABEL',
	release_channel: 'OPENDJ_BUILD_CHANNEL',
	evidence_written_at_utc: 'OPENDJ_BUILD_EVIDENCE_AT_UTC'
};

export function readBuildStamp(file: string): BuildStamp {
	try {
		const parsed = JSON.parse(fs.readFileSync(file, 'utf8')) as Record<string, unknown>;
		const stamp: BuildStamp = {};
		for (const key of Object.keys(STAMP_ENV) as (keyof BuildStamp)[]) {
			const value = parsed[key];
			if (typeof value === 'string' && value !== '') stamp[key] = value;
		}
		return stamp;
	} catch {
		return {};
	}
}

/** Same JSON shape the Tauri shell injects as OPENDJ_SHELL_BUILD, plus the shell kind. */
export function shellBuildIdentity(appVersion: string, stamp: BuildStamp): Record<string, unknown> {
	return {
		stamped: stamp.git_sha !== undefined,
		app_version: appVersion,
		git_sha: stamp.git_sha ?? null,
		git_sha_full: stamp.git_sha_full ?? null,
		git_branch: stamp.git_branch ?? null,
		git_dirty: stamp.git_dirty === undefined ? null : stamp.git_dirty === '1' || stamp.git_dirty === 'true',
		built_at_utc: stamp.built_at_utc ?? null,
		lane_label: stamp.lane_label ?? null,
		release_channel: stamp.release_channel ?? null,
		evidence_written_at_utc: stamp.evidence_written_at_utc ?? null,
		shell: 'electron'
	};
}

// ----- origins ------------------------------------------------------------------
/** The bundled bootstrap page's scheme and host (Tauri's was tauri://localhost). */
export const APP_SCHEME = 'opendj';
export const APP_HOST = 'app';
export const APP_ORIGIN = `${APP_SCHEME}://${APP_HOST}`;

const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '[::1]']);

/** Loopback http, the same scope the Tauri capabilities grant (`http://127.0.0.1:*`, `http://localhost:*`). */
export function isLoopbackHttp(url: string): boolean {
	let parsed: URL;
	try {
		parsed = new URL(url);
	} catch {
		return false;
	}
	return parsed.protocol === 'http:' && LOOPBACK_HOSTS.has(parsed.hostname);
}

export function isAppPage(url: string): boolean {
	try {
		const parsed = new URL(url);
		return parsed.protocol === `${APP_SCHEME}:` && parsed.host === APP_HOST;
	} catch {
		return false;
	}
}

/** May a frame at `url` use the native bridge, and may the window navigate there? */
export function isTrustedPage(url: string): boolean {
	return isLoopbackHttp(url) || isAppPage(url);
}

/** Chromium permissions granted to trusted pages; everything else is refused. */
export const GRANTED_PERMISSIONS: ReadonlySet<string> = new Set([
	// Web MIDI: a stopgap until MIDI moves into the Rust audio engine (D9).
	'midi',
	'midiSysex',
	// Output device ids for AudioContext.setSinkId (two-device cue, CUEOUT-09).
	'media',
	'speaker-selection'
]);

export function permissionAllowed(permission: string, requestingUrl: string): boolean {
	return GRANTED_PERMISSIONS.has(permission) && isLoopbackHttp(requestingUrl);
}

/** `openExternal` accepts only web URLs; never file:, never custom schemes. */
export function isExternalUrlAllowed(url: string): boolean {
	try {
		const parsed = new URL(url);
		return parsed.protocol === 'https:' || parsed.protocol === 'http:';
	} catch {
		return false;
	}
}

// ----- page globals -----------------------------------------------------------
export interface PageInit {
	/** OPENDJ_ENGINE_ORIGIN; null only before the engine is known. */
	engineOrigin: string | null;
	shellBuild: Record<string, unknown>;
	/** Present only while the supervisor has declared the engine dead. */
	supervisor: { engine: 'dead'; exit_code: number; lock_pid: number; lock_port: number; health_port: number } | null;
}
