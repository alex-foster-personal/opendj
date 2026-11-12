/**
 * The desktop shell's native features, whichever shell hosts this page.
 *
 * The SPA is served to browser tabs, the Tauri shell (WKWebView) and the
 * Electron shell (Chromium) alike. Four things need the host app rather than
 * the engine: a folder picker, opening a URL in the system browser, quitting,
 * and installing an update. This module is the one place that knows how each
 * shell offers them (ADR NEW electron-desktop-shell, decision D5):
 *
 * - Electron: `window.opendjShell`, exposed by apps/desktop/electron's
 *   preload. The main process re-checks the calling page's origin.
 * - Tauri: `__TAURI_INTERNALS__` plus the plugin packages, imported
 *   dynamically so they stay out of the browser bundle. Removed at cutover.
 * - A browser tab: none of them. Callers keep their browser behavior.
 *
 * Detection reads plain globals and never imports a shell package eagerly,
 * which keeps the thin-shell rule (apps/desktop/README.md).
 */

export type NativeShellKind = 'electron' | 'tauri';

export type ShellUpdateProgress =
	| { phase: 'checking' }
	| { phase: 'downloading'; received: number; total: number | null }
	| { phase: 'installing' }
	| { phase: 'restarting' };

export type ShellUpdateOutcome =
	{ kind: 'no-update' } | { kind: 'installed' } | { kind: 'refused'; reason: string };

/** What apps/desktop/electron/src/preload.ts exposes. */
interface ElectronShellBridge {
	kind: 'electron';
	version: number;
	pickFolder(options?: { title?: string }): Promise<string | null>;
	openExternal(url: string): Promise<void>;
	exit(code?: number): Promise<void>;
	relaunch(): Promise<void>;
	/** Absent in shells built before the engine choice moved to disk. */
	setAudioEngineChoice?(choice: string): Promise<void>;
	applyUpdate(onProgress?: (progress: ShellUpdateProgress) => void): Promise<ShellUpdateOutcome>;
	/** Version 2+: the docked Spotify side player (SPOT-6555b). */
	sidePlayer?: {
		open(url: string, options?: { shrinkMain?: boolean }): Promise<void>;
		setCollapsed(collapsed: boolean): Promise<void>;
		close(): Promise<void>;
		importBrowserLogins(): Promise<SideLoginImportOutcome>;
	};
	/** Extra labelled windows such as the library second screen. */
	windows?: {
		open(label: WindowLabel): Promise<void>;
		close(label: WindowLabel): Promise<void>;
		focus(label: WindowLabel): Promise<void>;
	};
	listCoreAudioOutputs(): Promise<unknown[]>;
}

export type WindowAction = 'open' | 'close' | 'focus';
export type WindowLabel = 'second-screen';
export const WINDOW_LABELS: readonly WindowLabel[] = ['second-screen'];

export type SideLoginImportOutcome =
	| { kind: 'imported'; browser: string; cookies: number; sites: string[] }
	| { kind: 'refused'; reason: string };

/** The global Tauri v2 injects into every window it owns. */
const TAURI_GLOBAL = '__TAURI_INTERNALS__';
/** The global the Electron preload exposes. */
const ELECTRON_GLOBAL = 'opendjShell';

// `globalThis` is assignable to this without a cast, and tests pass plain objects.
export type Scope = { readonly [key: string]: unknown };

export function electronBridge(scope: Scope): ElectronShellBridge | null {
	const candidate = scope[ELECTRON_GLOBAL] as Partial<ElectronShellBridge> | null | undefined;
	if (candidate === null || typeof candidate !== 'object') return null;
	return candidate.kind === 'electron' ? (candidate as ElectronShellBridge) : null;
}

/** Which desktop shell hosts this page, or null in a browser tab. */
export function nativeShellKind(scope: Scope = globalThis): NativeShellKind | null {
	if (electronBridge(scope) !== null) return 'electron';
	if (scope[TAURI_GLOBAL] !== undefined && scope[TAURI_GLOBAL] !== null) return 'tauri';
	return null;
}

/** OS directory picker. Null when cancelled; throws when there is no shell. */
export async function pickFolder(
	title = 'Choose a folder',
	scope: Scope = globalThis
): Promise<string | null> {
	const electron = electronBridge(scope);
	if (electron !== null) return electron.pickFolder({ title });
	if (nativeShellKind(scope) === 'tauri') {
		const { open } = await import('@tauri-apps/plugin-dialog');
		const selected = await open({ directory: true, multiple: false, title });
		return typeof selected === 'string' ? selected : null;
	}
	throw new Error('no desktop shell: this page is running in a browser tab');
}

/**
 * Open a URL in the system browser. OAuth and other interactive sign-in flows
 * use this path in desktop shells; loopback polling or device-code completion
 * belongs to the sign-in module, not this bridge.
 */
export async function openExternal(url: string, scope: Scope = globalThis): Promise<void> {
	const electron = electronBridge(scope);
	if (electron !== null) return electron.openExternal(url);
	if (nativeShellKind(scope) === 'tauri') {
		const { openUrl } = await import('@tauri-apps/plugin-opener');
		return openUrl(url);
	}
	throw new Error('no desktop shell: this page is running in a browser tab');
}

/** Quit the app after the quit gate confirmed it (INSTALL-21). */
export async function exitApp(code = 0, scope: Scope = globalThis): Promise<void> {
	const electron = electronBridge(scope);
	if (electron !== null) return electron.exit(code);
	if (nativeShellKind(scope) === 'tauri') {
		const { exit } = await import('@tauri-apps/plugin-process');
		return exit(code);
	}
	throw new Error('no desktop shell: this page is running in a browser tab');
}

/**
 * SECONDSCREEN-11715: open or focus the second-screen library window. Only the
 * Tauri shell has it; Electron and a browser tab refuse.
 */
export async function openSecondScreen(scope: Scope = globalThis): Promise<void> {
	if (electronBridge(scope) !== null || nativeShellKind(scope) !== 'tauri') {
		throw new Error('the library window needs the desktop app');
	}
	return controlShellWindow('open', 'second-screen', scope);
}

/**
 * Electron's install path; null when this page is not in Electron, so the
 * caller keeps its Tauri or browser path. The shell verifies and installs;
 * nothing parsed here is handed to it.
 */
export function electronApplyUpdate(
	onProgress: (progress: ShellUpdateProgress) => void,
	scope: Scope = globalThis
): Promise<ShellUpdateOutcome> | null {
	const electron = electronBridge(scope);
	return electron === null ? null : electron.applyUpdate(onProgress);
}

/** The global the Electron preload sets to the audio engine choice it saved. */
const ELECTRON_ENGINE_CHOICE_GLOBAL = 'OPENDJ_AUDIO_ENGINE_CHOICE';

/**
 * The audio engine choice the Electron shell saved to disk, or null when this
 * page is not in Electron or nothing was saved. The page's own localStorage
 * cannot keep it: the engine binds a new port, so a new origin, each launch.
 */
export function electronSavedAudioEngineChoice(scope: Scope = globalThis): string | null {
	if (electronBridge(scope) === null) return null;
	const saved = scope[ELECTRON_ENGINE_CHOICE_GLOBAL];
	return typeof saved === 'string' ? saved : null;
}

/**
 * Save the audio engine choice in the Electron shell; null when this page is
 * not in Electron. Rejects when the shell predates the bridge call, so a
 * choice that would not survive a relaunch is never reported as saved.
 */
export function electronSaveAudioEngineChoice(
	choice: string,
	scope: Scope = globalThis
): Promise<void> | null {
	const electron = electronBridge(scope);
	if (electron === null) return null;
	if (typeof electron.setAudioEngineChoice !== 'function') {
		return Promise.reject(
			new Error('this Electron shell cannot save the audio engine choice; update the app')
		);
	}
	return electron.setAudioEngineChoice(choice);
}

/** CoreAudio output descriptors for Electron on macOS; null in browser tabs and Tauri. */
export async function listCoreAudioOutputs(scope: Scope = globalThis): Promise<unknown[] | null> {
	const electron = electronBridge(scope);
	if (electron === null || typeof electron.listCoreAudioOutputs !== 'function') return null;
	return electron.listCoreAudioOutputs();
}

/**
 * Open, close or focus a labelled extra window through the desktop shell.
 * A browser tab is refused with a reason that names the shell, never ignored.
 */
export async function controlShellWindow(
	action: WindowAction,
	label: WindowLabel,
	scope: Scope = globalThis
): Promise<void> {
	const electron = electronBridge(scope);
	if (electron !== null) {
		const windows = electron.windows;
		const fn = windows === undefined ? undefined : windows[action];
		if (typeof fn !== 'function') {
			throw new Error('this Electron shell cannot control extra windows; update the Open DJ app');
		}
		await fn(label);
		return;
	}
	if (nativeShellKind(scope) === 'tauri') {
		const { invoke } = await import('@tauri-apps/api/core');
		await invoke('control_shell_window', { action, label });
		return;
	}
	throw new Error(
		'no desktop shell: the window command needs the Open DJ desktop shell, not a browser tab'
	);
}
