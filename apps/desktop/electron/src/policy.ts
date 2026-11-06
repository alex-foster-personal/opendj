// Pure shell policy: diagnostic flags, window size, build identity, which
// origins may use the native bridge, and what gets injected into each page.
// Everything here is free of Electron so node:test covers it directly.

import * as fs from 'node:fs';
import * as path from 'node:path';

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

// ----- Chromium CDP (D10 / ELECTRON-6877, issue #6873) --------------------------
/** Every Chromium switch that opens CDP: a TCP port, its bind address, or the fd 3/4 pipe. */
export const REMOTE_DEBUGGING_SWITCHES = ['remote-debugging-port', 'remote-debugging-address', 'remote-debugging-pipe'] as const;

/** True when argv carries Chromium remote-debugging switches (any form Electron accepts). */
export function argvRequestsRemoteDebugging(args: readonly string[]): boolean {
	for (let index = 0; index < args.length; index += 1) {
		const arg = args[index];
		if (arg === undefined) continue;
		for (const switchName of REMOTE_DEBUGGING_SWITCHES) {
			const prefix = `--${switchName}=`;
			if (arg === `--${switchName}` || arg.startsWith(prefix)) return true;
		}
		const next = args[index + 1];
		if (arg === '--remote-debugging-port' && next !== undefined && /^\d{1,5}$/.test(next)) {
			return true;
		}
	}
	return false;
}

export const PACKAGED_REMOTE_DEBUGGING_REFUSAL =
	'Open DJ refuses Chromium remote debugging (--remote-debugging-port, --remote-debugging-address or --remote-debugging-pipe) in packaged builds; use an unpackaged dev build with OPENDJ_REMOTE_DEBUGGING_PORT instead.';

/**
 * When non-null, the shell must strip any remote-debugging switches and exit 1
 * after printing this message. `chromiumHasRemoteDebugging` covers switches
 * Chromium parsed before the main module ran.
 */
export function packagedRemoteDebuggingRefusal(
	isPackaged: boolean,
	args: readonly string[],
	chromiumHasRemoteDebugging: boolean
): string | null {
	if (!isPackaged) return null;
	if (!argvRequestsRemoteDebugging(args) && !chromiumHasRemoteDebugging) return null;
	return PACKAGED_REMOTE_DEBUGGING_REFUSAL;
}

/** Port from `OPENDJ_REMOTE_DEBUGGING_PORT` for unpackaged dev builds only. */
export function devCdpPortFromEnv(isPackaged: boolean, envValue: string | undefined): string | null {
	if (isPackaged || envValue === undefined) return null;
	return /^\d{1,5}$/.test(envValue) ? envValue : null;
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

// ----- data dir (#6068) -------------------------------------------------------
/**
 * `$HOME/Library/Application Support`, the folder Tauri's `app_data_dir()`
 * resolves on macOS. Electron's `app.getPath('appData')` asks Foundation,
 * which ignores `$HOME` (measured Tue 3 Nov 2026), so a HOME-redirected
 * acceptance run would otherwise open the real library.
 */
/** The product's app name, which Electron also uses for the Chromium userData folder and so the single-instance lock. */
export const DEFAULT_APP_NAME = 'Open DJ';

/**
 * The app name from the packed package.json: its productName when it is the
 * product's or a lane's (`Open DJ (B)`, scripts/desktop_lane_config.py), else
 * the product's. A lane must not share the product's Chromium profile or
 * single-instance lock, or launching it beside the product only focuses the
 * product (parity matrix row 65, ELECTRON-8401).
 */
export function appNameFromPackage(pkg: unknown): string {
	const name = typeof pkg === 'object' && pkg !== null ? (pkg as { productName?: unknown }).productName : undefined;
	return typeof name === 'string' && /^Open DJ( \([A-Za-z0-9]+\))?$/.test(name) ? name : DEFAULT_APP_NAME;
}

export function macAppSupportDir(home: string): string {
	return path.join(home, 'Library', 'Application Support');
}

function hasParentComponent(raw: string): boolean {
	return raw.split(/[\\/]/).includes('..');
}

/** True when `inner` is `outer` or below it, component by component. */
function isWithin(inner: string, outer: string): boolean {
	const rel = path.relative(outer, inner);
	return rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel));
}

/**
 * Realpath of the longest existing ancestor of `target`, with the missing
 * components appended again. Creates nothing.
 */
function resolveExistingComponents(target: string): string {
	const missing: string[] = [];
	let existing = target;
	for (;;) {
		try {
			fs.lstatSync(existing);
			break;
		} catch (error) {
			if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw new Error(`cannot read ${existing}: ${(error as Error).message}`);
			const parent = path.dirname(existing);
			if (parent === existing) throw new Error(`no existing ancestor of ${target}`);
			missing.unshift(path.basename(existing));
			existing = parent;
		}
	}
	return path.join(fs.realpathSync(existing), ...missing);
}

/**
 * Refuse a data dir outside `home`, lexically or through a symlink, before
 * anything opens the store: the port of the Tauri shell's
 * `validate_data_dir_contained_in_home`. Throws with the reason.
 */
export function validateDataDirContainedInHome(dataDir: string, home: string | undefined): void {
	if (home === undefined || home.trim() === '' || !path.isAbsolute(home)) throw new Error('HOME is unset or not an absolute path');
	if (hasParentComponent(home)) throw new Error('HOME contains a parent (..) path component');
	if (!path.isAbsolute(dataDir)) throw new Error(`data directory ${dataDir} is not an absolute path`);
	if (hasParentComponent(dataDir)) throw new Error(`data directory ${dataDir} contains a parent (..) path component`);
	if (!isWithin(dataDir, home)) throw new Error(`${dataDir} is not inside HOME ${home}`);
	const resolvedHome = resolveExistingComponents(home);
	const resolvedData = resolveExistingComponents(dataDir);
	if (!isWithin(resolvedData, resolvedHome)) {
		throw new Error(`${dataDir} resolves to ${resolvedData}, which is not inside HOME ${resolvedHome}`);
	}
}

// ----- application menu (macOS) -------------------------------------------------
/** The subset of Electron's MenuItemConstructorOptions this template uses; kept Electron-free. */
export interface MenuItemSpec {
	role?: string;
	label?: string;
	type?: 'separator';
	submenu?: MenuItemSpec[];
}

/** Roles that reload the page or open developer tools: never in a packaged build. */
export const DEVELOPER_MENU_ROLES: ReadonlySet<string> = new Set([
	'reload',
	'forceReload',
	'toggleDevTools',
	'resetZoom',
	'zoomIn',
	'zoomOut'
]);

const separator: MenuItemSpec = { type: 'separator' };

/**
 * The macOS menu bar: the menus Tauri 2 installs by default (app, File, Edit,
 * View with full screen only, Window, Help), so Cmd-Q, Cmd-W, Cmd-C/V/X/A
 * and Cmd-M behave as they did. Without an explicit menu Electron installs
 * its own, whose View menu reloads the page on Cmd-R (stopping the decks
 * mid-set) and opens developer tools in a packaged build. An unpackaged
 * build keeps reload and developer tools for the dev loop.
 */
export function macMenuTemplate(productName: string, isPackaged: boolean): MenuItemSpec[] {
	const view: MenuItemSpec[] = [{ role: 'togglefullscreen' }];
	if (!isPackaged) view.unshift({ role: 'reload' }, { role: 'forceReload' }, { role: 'toggleDevTools' }, separator);
	return [
		{
			label: productName,
			submenu: [
				{ role: 'about' },
				separator,
				{ role: 'services' },
				separator,
				{ role: 'hide' },
				{ role: 'hideOthers' },
				{ role: 'unhide' },
				separator,
				{ role: 'quit' }
			]
		},
		{ label: 'File', submenu: [{ role: 'close' }] },
		{
			label: 'Edit',
			submenu: [
				{ role: 'undo' },
				{ role: 'redo' },
				separator,
				{ role: 'cut' },
				{ role: 'copy' },
				{ role: 'paste' },
				{ role: 'selectAll' }
			]
		},
		{ label: 'View', submenu: view },
		{ role: 'windowMenu' },
		{ role: 'help', submenu: [] }
	];
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

// IPv6 loopback is deliberately absent: the Tauri capabilities do not grant it (EL-17-F13).
const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost']);

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

// ----- renderer crash recovery (parity row 72, issue #9390) -----------------------

/** Reload incidents allowed in a rolling window before the shell gives up. */
export const RENDERER_RELOAD_LIMIT = 3;
export const RENDERER_RELOAD_WINDOW_MS = 60_000;

export type RendererGoneAction = 'reload' | 'give-up' | 'ignore';

export interface RendererGone {
	/** Electron `RenderProcessGoneDetails.reason` (`crashed`, `oom`, `clean-exit`, ...). */
	reason: string;
	nowMs: number;
	/** Timestamps of reload decisions still inside the rolling window. */
	reloadsMs: readonly number[];
	/** Shell is quitting: a renderer exit is expected. */
	quitting: boolean;
}

/**
 * Decide whether to reload, give up, or ignore a dead renderer. `reloadsMs` is
 * the history carried forward; a timestamp older than `RENDERER_RELOAD_WINDOW_MS`
 * is dropped (strict `<` on the age).
 */
export function rendererGoneDecision(gone: RendererGone): { action: RendererGoneAction; reloadsMs: number[] } {
	const recent = gone.reloadsMs.filter((at) => gone.nowMs - at < RENDERER_RELOAD_WINDOW_MS);
	if (gone.reason === 'clean-exit' || gone.quitting) return { action: 'ignore', reloadsMs: recent };
	if (recent.length >= RENDERER_RELOAD_LIMIT) return { action: 'give-up', reloadsMs: recent };
	return { action: 'reload', reloadsMs: [...recent, gone.nowMs] };
}

/** Page to load after a crash: current URL if trusted, else last trusted, else bootstrap. */
export function rendererReloadTarget(currentUrl: string, lastTrustedUrl: string | null): string {
	if (isTrustedPage(currentUrl)) return currentUrl;
	if (lastTrustedUrl !== null && isTrustedPage(lastTrustedUrl)) return lastTrustedUrl;
	return `${APP_ORIGIN}/index.html`;
}

/** Chromium permissions granted to trusted pages; everything else is refused. */
export const GRANTED_PERMISSIONS: ReadonlySet<string> = new Set([
	// Web MIDI: a stopgap until MIDI moves into the Rust audio engine (D9). No 'midiSysex':
	// Tauri's native_midi_send accepts three bounded bytes, and no map needs SysEx (EL-17-F14).
	'midi',
	// Output device ids for AudioContext.setSinkId (two-device cue, CUEOUT-09).
	'media',
	'speaker-selection'
]);

export function permissionAllowed(permission: string, requestingUrl: string): boolean {
	return GRANTED_PERMISSIONS.has(permission) && isLoopbackHttp(requestingUrl);
}

const OPEN_DJ_LOCALHOST_PREFIX = 'http://opendj.localhost:';

function isFourOrFiveDigitPort(port: string): boolean {
	return /^[0-9]{4}$/.test(port) || /^[0-9]{5}$/.test(port);
}

/** Raw-string shape Tauri glob-matches: four or five ASCII digits then `/`. */
function matchesOpenDjLocalhostRaw(url: string): boolean {
	if (!url.startsWith(OPEN_DJ_LOCALHOST_PREFIX)) return false;
	const rest = url.slice(OPEN_DJ_LOCALHOST_PREFIX.length);
	const slashIdx = rest.indexOf('/');
	if (slashIdx < 0) return false;
	const portPart = rest.slice(0, slashIdx);
	return isFourOrFiveDigitPort(portPart);
}

function matchesOpenDjLocalhostParsed(parsed: URL): boolean {
	return (
		parsed.protocol === 'http:' &&
		parsed.hostname === 'opendj.localhost' &&
		parsed.username === '' &&
		parsed.password === '' &&
		isFourOrFiveDigitPort(parsed.port)
	);
}

/** The named local origin `http://opendj.localhost:<4-5 digit port>/...`, matched raw and parsed as the Tauri opener does. */
export function isOpenDjLocalhostUrl(url: string, parsed: URL): boolean {
	return matchesOpenDjLocalhostRaw(url) && matchesOpenDjLocalhostParsed(parsed);
}

/**
 * What the system browser may be asked to open: any https URL without
 * userinfo (Google sign-in, the BlackHole installer, the privacy policy, and
 * the Beatport / Bandcamp purchase links the library renders), plus the named
 * local origin `http://opendj.localhost:<4-5 digit port>/...` the Tauri
 * opener allows. Plain http elsewhere, userinfo, file:, data:, javascript:
 * and every custom scheme are refused. Nothing allowed here loads inside the
 * shell's window: classifyMainFrameNavigation keeps the window on trusted
 * pages only.
 */
export function isExternalUrlAllowed(url: string): boolean {
	let parsed: URL;
	try {
		parsed = new URL(url);
	} catch {
		return false;
	}
	if (parsed.username !== '' || parsed.password !== '') return false;
	if (isOpenDjLocalhostUrl(url, parsed)) return true;
	return parsed.protocol === 'https:' && parsed.hostname !== '';
}

/** How the main window should treat a main-frame navigation, redirect, or window.open URL. */
export type MainFrameNavigationAction = 'allow' | 'open-external' | 'deny';

/**
 * Trusted pages stay in the shell; allow-listed externals open in the system browser;
 * everything else is refused without opening externally. Never throws.
 */
export function classifyMainFrameNavigation(url: string): MainFrameNavigationAction {
	if (isTrustedPage(url)) return 'allow';
	if (isExternalUrlAllowed(url)) return 'open-external';
	return 'deny';
}

// ----- page globals -----------------------------------------------------------
export interface PageInit {
	/** OPENDJ_ENGINE_ORIGIN; null only before the engine is known. */
	engineOrigin: string | null;
	shellBuild: Record<string, unknown>;
	/** OPENDJ_PERF_PROBE: true when the shell was launched with OPENDJ_PERF_PROBE=1 (PERFPROBE-02). */
	perfProbe: boolean;
	/** Present only while the supervisor has declared the engine dead. */
	supervisor: { engine: 'dead'; exit_code: number; lock_pid: number; lock_port: number; health_port: number } | null;
	/** OPENDJ_AUDIO_ENGINE_CHOICE: the audio engine last chosen in Settings, or null when none was. */
	audioEngineChoice: AudioEngineChoice | null;
}

// ----- audio engine choice -------------------------------------------------------
// The page remembers its engine choice in localStorage, but the engine binds a new
// port each launch, so the page's origin, and with it that storage, starts empty
// every time. The shell keeps the choice on disk instead (NAE-8118).
export type AudioEngineChoice = 'webaudio' | 'rust';
export const AUDIO_ENGINE_CHOICE_FILE = 'audio-engine-choice.json';

export function isAudioEngineChoice(value: unknown): value is AudioEngineChoice {
	return value === 'webaudio' || value === 'rust';
}

/** The saved choice; null when none was saved. Throws on a file it cannot read. */
export function readAudioEngineChoice(file: string): AudioEngineChoice | null {
	let raw: string;
	try {
		raw = fs.readFileSync(file, 'utf8');
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null;
		throw error;
	}
	const choice = (JSON.parse(raw) as { choice?: unknown } | null)?.choice;
	if (!isAudioEngineChoice(choice)) throw new Error(`${file}: choice ${JSON.stringify(choice)} is not one of webaudio, rust`);
	return choice;
}

/** Save the choice atomically (write a sibling, then rename over the file). */
export function writeAudioEngineChoice(file: string, choice: unknown): AudioEngineChoice {
	if (!isAudioEngineChoice(choice)) throw new Error(`audio engine choice must be webaudio|rust, got ${String(choice)}`);
	fs.mkdirSync(path.dirname(file), { recursive: true });
	const temp = `${file}.${process.pid}.tmp`;
	fs.writeFileSync(temp, `${JSON.stringify({ choice })}\n`);
	fs.renameSync(temp, file);
	return choice;
}
