// Preload for the one app window. Runs sandboxed, before any page script, on
// every document the window loads, which is exactly the guarantee Tauri's
// `initialization_script` gave (ADR-0048).
//
// Two jobs, nothing else:
// 1. Put the shell's globals into the page's own world (P14): the engine
//    origin, the build identity, the quit hook default, the pending shell
//    error queue, and the dead-engine supervisor state when there is one.
// 2. Expose `window.opendjShell`, the four native features the UI needs
//    (folder picker, open in browser, exit/relaunch, updater). The main
//    process re-checks the calling frame's origin on every call.
//
// Sandboxed preloads may only require 'electron', so this file imports nothing
// from the rest of the shell.

import { contextBridge, ipcRenderer } from 'electron';

type Progress =
	| { phase: 'checking' }
	| { phase: 'downloading'; received: number; total: number | null }
	| { phase: 'installing' }
	| { phase: 'restarting' };

interface PageInit {
	engineOrigin: string | null;
	shellBuild: Record<string, unknown>;
	supervisor: Record<string, unknown> | null;
}

const init = ipcRenderer.sendSync('opendj:page-init') as PageInit | null;

// null means the main process judged this page untrusted: no globals, no bridge.
if (init !== null) {
	contextBridge.executeInMainWorld({
		func: (state: PageInit) => {
			const scope = globalThis as unknown as Record<string, unknown>;
			if (state.engineOrigin !== null) scope.OPENDJ_ENGINE_ORIGIN = state.engineOrigin;
			scope.OPENDJ_SHELL_BUILD = state.shellBuild;
			scope.__OPENDJ_requestQuit = scope.__OPENDJ_requestQuit || function () {};
			const pending = (scope.__OPENDJ_PENDING_SHELL_ERRORS__ as unknown[] | undefined) ?? [];
			scope.__OPENDJ_PENDING_SHELL_ERRORS__ = pending;
			scope.__OPENDJ_enqueueShellClientError = function (kind: string, message: string, context?: unknown) {
				pending.push({ kind, message, context: context || { source: 'shell-webview' } });
			};
			if (state.supervisor !== null) scope.__OPENDJ_ENGINE_SUPERVISOR__ = state.supervisor;
		},
		args: [init]
	});

	contextBridge.exposeInMainWorld('opendjShell', {
		kind: 'electron',
		version: 1,
		pickFolder: (options?: { title?: string }): Promise<string | null> =>
			ipcRenderer.invoke('opendj:pick-folder', { title: options?.title }),
		openExternal: (url: string): Promise<void> => ipcRenderer.invoke('opendj:open-external', String(url)),
		exit: (code = 0): Promise<void> => ipcRenderer.invoke('opendj:exit', Number(code)),
		relaunch: (): Promise<void> => ipcRenderer.invoke('opendj:relaunch'),
		applyUpdate: async (
			onProgress?: (progress: Progress) => void
		): Promise<{ kind: 'no-update' } | { kind: 'installed' } | { kind: 'refused'; reason: string }> => {
			const listener = (_event: unknown, progress: Progress): void => {
				if (typeof onProgress === 'function') onProgress(progress);
			};
			ipcRenderer.on('opendj:update-progress', listener);
			try {
				return await ipcRenderer.invoke('opendj:update-apply');
			} finally {
				ipcRenderer.removeListener('opendj:update-progress', listener);
			}
		}
	});
}
