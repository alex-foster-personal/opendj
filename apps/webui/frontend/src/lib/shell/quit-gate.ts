/**
 * INSTALL-21: desktop-shell quit gate. The shell (Tauri or Electron) intercepts
 * Cmd-Q and window close and calls `globalThis.__OPENDJ_requestQuit`; this
 * module owns the product decision.
 */

import { subscribe, TOPIC_SHELL_QUIT, type Unsubscribe } from '$lib/api/events-bus';
import { reportClientError } from '$lib/client-error-reporting';
import { flushPerformanceSessionSnapshot } from '$lib/rb/performance-session.svelte';
import { queryPerformanceState } from '$lib/rb/performance-ipc.svelte';
import { detectSurface, type ShellScope } from '$lib/rb/usage-heartbeat';
import { closeQuitConfirm, isQuitConfirmOpen, openQuitConfirm } from './quit-gate-state';
import { exitApp } from './native-shell';
import { needsQuitConfirmation } from './needs-quit-confirmation';
import { planQuitRequest } from './quit-gate-logic';

export interface QuitRequestOptions {
	force?: boolean;
	source?: string;
}

type QuitGateDeps = {
	query: typeof queryPerformanceState;
	flushSnapshot: typeof flushPerformanceSessionSnapshot;
	exitShell: () => Promise<void>;
	detect: (scope: ShellScope) => ReturnType<typeof detectSurface>;
	subscribeShellQuit: (listener: () => void) => Unsubscribe;
	document: Document;
	window: Window & ShellScope;
};

let confirming = false;

async function exitShellDefault(): Promise<void> {
	await exitApp(0);
}

function isConfirmChord(event: KeyboardEvent): boolean {
	const key = event.key.toLowerCase();
	if (event.key === 'Enter') return true;
	if (key === 'q' && (event.metaKey || event.ctrlKey)) return true;
	return false;
}

function onDialogKeydown(event: KeyboardEvent): void {
	if (!isQuitConfirmOpen()) return;
	if (event.key === 'Escape') {
		event.preventDefault();
		event.stopPropagation();
		closeQuitConfirm();
		return;
	}
	if (isConfirmChord(event)) {
		event.preventDefault();
		event.stopPropagation();
		void confirmQuit();
	}
}

export async function confirmQuit(deps?: Pick<QuitGateDeps, 'flushSnapshot' | 'exitShell'>): Promise<void> {
	if (confirming) return;
	confirming = true;
	closeQuitConfirm();
	const flush = deps?.flushSnapshot ?? flushPerformanceSessionSnapshot;
	const exit = deps?.exitShell ?? exitShellDefault;
	try {
		flush();
		await exit();
	} catch (error) {
		// exit() rejects when the ACL refuses process:allow-exit (issue #3058),
		// or when the Electron shell refuses a page outside its trusted origins.
		// Report deliberately, under the quit-gate source, instead of letting it
		// surface only as a generic window.onunhandledrejection accident: that
		// kept the failure diagnosable only by luck, with no quit-path context.
		// Kind stays 'unhandled-rejection' on purpose -- ops already greps that
		// exact string for this failure mode.
		reportClientError(error, { source: 'quit-gate', route: 'lifecycle/quit' }, 'unhandled-rejection');
	} finally {
		confirming = false;
	}
}

export function cancelQuit(): void {
	closeQuitConfirm();
}

export function handleQuitRequest(
	options: QuitRequestOptions = {},
	deps?: Partial<QuitGateDeps>
): void {
	const resolved: QuitGateDeps = {
		query: deps?.query ?? queryPerformanceState,
		flushSnapshot: deps?.flushSnapshot ?? flushPerformanceSessionSnapshot,
		exitShell: deps?.exitShell ?? exitShellDefault,
		detect: deps?.detect ?? detectSurface,
		subscribeShellQuit: deps?.subscribeShellQuit ?? ((listener) => subscribe(TOPIC_SHELL_QUIT, () => listener())),
		document: deps?.document ?? document,
		window: deps?.window ?? (window as Window & ShellScope)
	};

	const action = planQuitRequest({
		...(options.force !== undefined ? { force: options.force } : {}),
		dialogOpen: isQuitConfirmOpen(),
		needsConfirmation: needsQuitConfirmation(resolved.query())
	});
	if (action === 'confirm') {
		void confirmQuit(resolved);
		return;
	}
	const active = resolved.document.activeElement;
	const focusTarget =
		active instanceof HTMLElement ? active : resolved.document.querySelector('main');
	openQuitConfirm(focusTarget instanceof HTMLElement ? focusTarget : null);
}

export function installQuitGate(deps?: Partial<QuitGateDeps>): () => void {
	const resolved: QuitGateDeps = {
		query: deps?.query ?? queryPerformanceState,
		flushSnapshot: deps?.flushSnapshot ?? flushPerformanceSessionSnapshot,
		exitShell: deps?.exitShell ?? exitShellDefault,
		detect: deps?.detect ?? detectSurface,
		subscribeShellQuit:
			deps?.subscribeShellQuit ?? ((listener) => subscribe(TOPIC_SHELL_QUIT, () => listener())),
		document: deps?.document ?? document,
		window: deps?.window ?? (window as Window & ShellScope)
	};

	if (resolved.detect(resolved.window) !== 'desktop-shell') {
		return () => undefined;
	}

	const requestQuit = (): void => handleQuitRequest({ source: 'shell' }, resolved);
	(
		resolved.window as Window & ShellScope & { __OPENDJ_requestQuit?: () => void }
	).__OPENDJ_requestQuit = requestQuit;

	const onKeydown = (event: KeyboardEvent): void => onDialogKeydown(event);
	resolved.document.addEventListener('keydown', onKeydown, true);

	const unsubscribe = resolved.subscribeShellQuit(() => {
		handleQuitRequest({ force: true, source: 'engine' }, resolved);
	});

	return () => {
		unsubscribe();
		resolved.document.removeEventListener('keydown', onKeydown, true);
	};
}
