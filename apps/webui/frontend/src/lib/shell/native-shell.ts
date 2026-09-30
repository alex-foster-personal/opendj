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
	| { kind: 'no-update' }
	| { kind: 'installed' }
	| { kind: 'refused'; reason: string };

/** What apps/desktop/electron/src/preload.ts exposes. */
interface ElectronShellBridge {
	kind: 'electron';
	version: number;
	pickFolder(options?: { title?: string }): Promise<string | null>;
	openExternal(url: string): Promise<void>;
	exit(code?: number): Promise<void>;
	relaunch(): Promise<void>;
	applyUpdate(onProgress?: (progress: ShellUpdateProgress) => void): Promise<ShellUpdateOutcome>;
}

/** The global Tauri v2 injects into every window it owns. */
const TAURI_GLOBAL = '__TAURI_INTERNALS__';
/** The global the Electron preload exposes. */
const ELECTRON_GLOBAL = 'opendjShell';

// `globalThis` is assignable to this without a cast, and tests pass plain objects.
type Scope = { readonly [key: string]: unknown };

function electronBridge(scope: Scope): ElectronShellBridge | null {
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

/** Open a URL in the system browser (Google sign-in needs passkeys outside the webview). */
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
