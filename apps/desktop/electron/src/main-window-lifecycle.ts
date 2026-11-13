// Main window close and renderer crash handlers, extracted for node:test fakes.

import { RENDERER_RELOAD_LIMIT, rendererGoneDecision, rendererReloadTarget } from './policy';
import { EngineError } from './shell-log';

export interface PreventableEvent {
	preventDefault(): void;
	defaultPrevented: boolean;
}

export interface RenderProcessGoneDetails {
	reason: string;
	exitCode: number;
}

export interface LifecycleContents {
	getURL(): string;
	loadURL(target: string): Promise<void>;
	on(event: 'render-process-gone', listener: (event: unknown, details: RenderProcessGoneDetails) => void): void;
}

export interface LifecycleWindow {
	on(event: 'close', listener: (event: PreventableEvent) => void): void;
}

export interface MainWindowLifecycleState {
	quitAllowed: boolean;
	shuttingDown: Promise<void> | null;
	rendererReloadsMs: number[];
	lastTrustedUrl: string | null;
}

export interface MainWindowLifecycleDeps {
	appendShellLog(level: string, message: string): void;
	nowMs(): number;
	loadUrl(contents: LifecycleContents, target: string): void;
	requestQuitFromWebview(trigger: string): void;
	shutdownAll(reason: string): Promise<void>;
	reportFatal(error: unknown): void;
	getUrl(contents: LifecycleContents): string;
}

export function handleWindowClose(
	event: PreventableEvent,
	state: MainWindowLifecycleState,
	deps: MainWindowLifecycleDeps
): void {
	if (state.quitAllowed) return;
	event.preventDefault();
	deps.appendShellLog('shutdown', 'window-close requested; delegating to webview quit gate');
	deps.requestQuitFromWebview('window-close');
}

export function handleRenderProcessGone(
	details: RenderProcessGoneDetails,
	contents: LifecycleContents,
	state: MainWindowLifecycleState,
	deps: MainWindowLifecycleDeps
): void {
	const decision = rendererGoneDecision({
		reason: details.reason,
		nowMs: deps.nowMs(),
		reloadsMs: state.rendererReloadsMs,
		quitting: state.quitAllowed || state.shuttingDown !== null
	});
	state.rendererReloadsMs = decision.reloadsMs;
	if (decision.action === 'ignore') return;
	deps.appendShellLog('WARN', `renderer gone: ${details.reason} exitCode=${details.exitCode}`);
	if (decision.action === 'reload') {
		const target = rendererReloadTarget(deps.getUrl(contents), state.lastTrustedUrl);
		deps.appendShellLog('webview', `reloading after renderer crash: ${target}`);
		deps.loadUrl(contents, target);
		return;
	}
	const error = new EngineError(
		"Open DJ's window crashed repeatedly.",
		`The page renderer went away ${RENDERER_RELOAD_LIMIT + 1} times in 60 seconds (last: ${details.reason}, exit code ${details.exitCode}). See logs/engine.log.`
	);
	state.quitAllowed = true;
	void deps
		.shutdownAll('renderer crash loop')
		.catch((stopError: unknown) =>
			deps.appendShellLog('shutdown', `[ERROR] shell exit: ${(stopError as Error).message}`)
		)
		.finally(() => {
			deps.reportFatal(error);
		});
}

export function handleBeforeQuit(
	event: PreventableEvent,
	state: MainWindowLifecycleState,
	requestQuitFromWebview: (trigger: string) => void
): void {
	if (state.quitAllowed) return;
	event.preventDefault();
	requestQuitFromWebview('apple-event-quit');
}

export function handleWillQuit(event: PreventableEvent, state: MainWindowLifecycleState, exitApp: () => void): void {
	if (state.shuttingDown === null) {
		event.preventDefault();
		exitApp();
	}
}

export function wireMainWindowLifecycle(
	win: LifecycleWindow,
	contents: LifecycleContents,
	state: MainWindowLifecycleState,
	deps: MainWindowLifecycleDeps
): void {
	win.on('close', (event) => handleWindowClose(event, state, deps));
	contents.on('render-process-gone', (_event, details) => handleRenderProcessGone(details, contents, state, deps));
}
