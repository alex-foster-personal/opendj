// Main-window lifecycle wiring (issue #13309); policy math stays in policy.test.ts.

import * as assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	handleBeforeQuit,
	handleRenderProcessGone,
	handleWindowClose,
	type LifecycleContents,
	type LifecycleWindow,
	type MainWindowLifecycleDeps,
	type MainWindowLifecycleState,
	type PreventableEvent,
	type RenderProcessGoneDetails,
	wireMainWindowLifecycle
} from '../src/main-window-lifecycle';
import { APP_ORIGIN, RENDERER_RELOAD_LIMIT, rendererReloadTarget } from '../src/policy';

const anyPort = 40000 + (process.pid % 20000);
const TRUSTED = `http://127.0.0.1:${anyPort}/performance`;

class FakePreventableEvent implements PreventableEvent {
	defaultPrevented = false;
	preventDefault(): void {
		this.defaultPrevented = true;
	}
}

type RenderGoneListener = (event: unknown, details: RenderProcessGoneDetails) => void;
type CloseListener = (event: PreventableEvent) => void;

class FakeContents implements LifecycleContents {
	private renderListeners: RenderGoneListener[] = [];
	readonly loadUrls: string[] = [];
	private currentUrl: string;

	constructor(initialUrl: string) {
		this.currentUrl = initialUrl;
	}

	getURL(): string {
		return this.currentUrl;
	}

	loadURL(target: string): Promise<void> {
		this.loadUrls.push(target);
		this.currentUrl = target;
		return Promise.resolve();
	}

	on(event: 'render-process-gone', listener: RenderGoneListener): void {
		if (event === 'render-process-gone') this.renderListeners.push(listener);
	}

	emitRenderProcessGone(details: RenderProcessGoneDetails): void {
		for (const listener of this.renderListeners) listener(null, details);
	}
}

class FakeWindow implements LifecycleWindow {
	private closeListeners: CloseListener[] = [];

	on(event: 'close', listener: CloseListener): void {
		if (event === 'close') this.closeListeners.push(listener);
	}

	emitClose(): FakePreventableEvent {
		const event = new FakePreventableEvent();
		for (const listener of this.closeListeners) listener(event);
		return event;
	}
}

function baseState(overrides: Partial<MainWindowLifecycleState> = {}): MainWindowLifecycleState {
	return {
		quitAllowed: false,
		shuttingDown: null,
		rendererReloadsMs: [],
		lastTrustedUrl: null,
		...overrides
	};
}

function makeDeps(overrides: Partial<MainWindowLifecycleDeps> & { nowMs?: () => number } = {}): {
	deps: MainWindowLifecycleDeps;
	logs: string[];
	loadCalls: string[];
	quitTriggers: string[];
	shutdownReasons: string[];
	fatalErrors: unknown[];
} {
	const logs: string[] = [];
	const loadCalls: string[] = [];
	const quitTriggers: string[] = [];
	const shutdownReasons: string[] = [];
	const fatalErrors: unknown[] = [];
	let clock = 1_000_000;
	const deps: MainWindowLifecycleDeps = {
		appendShellLog: (level, message) => {
			logs.push(`${level}:${message}`);
		},
		nowMs: () => {
			const t = overrides.nowMs?.() ?? clock;
			return t;
		},
		loadUrl: (contents, target) => {
			loadCalls.push(target);
			void contents.loadURL(target);
		},
		requestQuitFromWebview: (trigger) => {
			quitTriggers.push(trigger);
		},
		shutdownAll: async (reason) => {
			shutdownReasons.push(reason);
		},
		reportFatal: (error) => {
			fatalErrors.push(error);
		},
		getUrl: (contents) => contents.getURL(),
		...overrides
	};
	return { deps, logs, loadCalls, quitTriggers, shutdownReasons, fatalErrors };
}

function wiredHarness(initialUrl = TRUSTED, stateOverrides?: Partial<MainWindowLifecycleState>) {
	const state = baseState({ lastTrustedUrl: TRUSTED, ...stateOverrides });
	const contents = new FakeContents(initialUrl);
	const win = new FakeWindow();
	const bag = makeDeps();
	wireMainWindowLifecycle(win, contents, state, bag.deps);
	return { state, contents, win, ...bag };
}

test('[if] the renderer process exits unexpectedly [then] the shell reloads a trusted page, [else stop]', () => {
	const state = baseState({ lastTrustedUrl: TRUSTED });
	const contents = new FakeContents(TRUSTED);
	const { deps, loadCalls, shutdownReasons, fatalErrors } = makeDeps();
	handleRenderProcessGone({ reason: 'crashed', exitCode: 1 }, contents, state, deps);
	assert.equal(loadCalls.length, 1);
	assert.equal(loadCalls[0], rendererReloadTarget(TRUSTED, TRUSTED));
	assert.equal(shutdownReasons.length, 0);
	assert.equal(fatalErrors.length, 0);
	assert.equal(state.rendererReloadsMs.length, 1);
});

test('[if] a replacement window is created after a renderer crash [then] loadURL restores the page, [else stop]', () => {
	const { state, contents, win, loadCalls, shutdownReasons } = wiredHarness();
	const first = contents;
	win.emitClose();
	contents.emitRenderProcessGone({ reason: 'crashed', exitCode: 1 });
	assert.equal(contents, first);
	assert.equal(loadCalls.length, 1);
	contents.emitRenderProcessGone({ reason: 'oom', exitCode: 2 });
	assert.equal(loadCalls.length, 2);
	assert.equal(shutdownReasons.length, 0);
	assert.equal(state.rendererReloadsMs.length, 2);
});

test('[if] the application is quitting normally [then] renderer exit does not reload, [else stop]', () => {
	const quitting = baseState({ quitAllowed: true, lastTrustedUrl: TRUSTED });
	const contents = new FakeContents(TRUSTED);
	const { deps, loadCalls } = makeDeps();
	handleRenderProcessGone({ reason: 'crashed', exitCode: 1 }, contents, quitting, deps);
	assert.equal(loadCalls.length, 0);

	const shutting = baseState({
		shuttingDown: Promise.resolve(),
		lastTrustedUrl: TRUSTED
	});
	const { deps: deps2, loadCalls: loads2 } = makeDeps();
	handleRenderProcessGone({ reason: 'crashed', exitCode: 1 }, contents, shutting, deps2);
	assert.equal(loads2.length, 0);

	const running = baseState({ lastTrustedUrl: TRUSTED });
	const { deps: deps3, loadCalls: loads3 } = makeDeps();
	handleRenderProcessGone({ reason: 'clean-exit', exitCode: 0 }, contents, running, deps3);
	assert.equal(loads3.length, 0);
});

test('[if] the user closes the window without quit allowed [then] close is prevented and quit is requested, [else stop]', () => {
	const state = baseState();
	const { deps, quitTriggers } = makeDeps();
	const event = new FakePreventableEvent();
	handleWindowClose(event, state, deps);
	assert.equal(event.defaultPrevented, true);
	assert.deepEqual(quitTriggers, ['window-close']);
});

test('[if] quit is already allowed [then] window close is not prevented, [else stop]', () => {
	const state = baseState({ quitAllowed: true });
	const { deps, quitTriggers } = makeDeps();
	const event = new FakePreventableEvent();
	handleWindowClose(event, state, deps);
	assert.equal(event.defaultPrevented, false);
	assert.equal(quitTriggers.length, 0);
});

test('[if] reload limit exceeded [then] fatal shutdown runs without another loadURL, [else stop]', async () => {
	const state = baseState({ lastTrustedUrl: TRUSTED });
	const contents = new FakeContents(TRUSTED);
	let now = 1_000_000;
	const { deps, loadCalls, shutdownReasons, fatalErrors } = makeDeps({ nowMs: () => now });
	for (let i = 0; i < RENDERER_RELOAD_LIMIT; i += 1) {
		handleRenderProcessGone({ reason: 'crashed', exitCode: 1 }, contents, state, deps);
		now += 1000;
	}
	assert.equal(loadCalls.length, RENDERER_RELOAD_LIMIT);
	handleRenderProcessGone({ reason: 'crashed', exitCode: 1 }, contents, state, deps);
	await new Promise<void>((resolve) => setImmediate(resolve));
	assert.equal(loadCalls.length, RENDERER_RELOAD_LIMIT);
	assert.equal(state.quitAllowed, true);
	assert.deepEqual(shutdownReasons, ['renderer crash loop']);
	assert.equal(fatalErrors.length, 1);
});

test('[if] quit is not allowed yet [then] before-quit prevents default and delegates, [else stop]', () => {
	const state = baseState();
	const quitTriggers: string[] = [];
	const event = new FakePreventableEvent();
	handleBeforeQuit(event, state, (trigger) => quitTriggers.push(trigger));
	assert.equal(event.defaultPrevented, true);
	assert.deepEqual(quitTriggers, ['apple-event-quit']);
});

test('bootstrap reload when current URL is not trusted', () => {
	const blank = 'about:blank';
	const state = baseState();
	const contents = new FakeContents(blank);
	const { deps, loadCalls } = makeDeps();
	handleRenderProcessGone({ reason: 'crashed', exitCode: 1 }, contents, state, deps);
	assert.equal(loadCalls[0], `${APP_ORIGIN}/index.html`);
});
