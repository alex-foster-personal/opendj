import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';
import * as vm from 'node:vm';

const electronRoot = path.join(__dirname, '..', '..');
const mainTs = fs.readFileSync(path.join(electronRoot, 'src', 'main.ts'), 'utf8');
const preloadTs = fs.readFileSync(path.join(electronRoot, 'src', 'preload.ts'), 'utf8');
const topBar = fs.readFileSync(
	path.join(electronRoot, '..', '..', 'webui', 'frontend', 'src', 'lib', 'components', 'rb', 'TopBar.svelte'),
	'utf8'
);

/** LINK-96: Electron shell pins the same reclaimed macOS title bar as Tauri. */
test('LINK-96 electron main window uses a hidden title bar on macOS only', () => {
	assert.match(
		mainTs,
		/process\.platform\s*===\s*['"]darwin['"][\s\S]*titleBarStyle:\s*['"]hidden['"][\s\S]*trafficLightPosition:\s*\{\s*x:\s*12,\s*y:\s*8\s*\}/
	);
	assert.match(
		mainTs,
		/process\.platform\s*===\s*['"]darwin['"][\s\S]*titleBarStyle[\s\S]*:\s*\{\s*\}\s*\)/
	);
});

test('LINK-96 electron preload sets data-titlebar overlay on macOS before page init', () => {
	const initAt = preloadTs.indexOf("ipcRenderer.sendSync('opendj:page-init')");
	assert.ok(initAt !== -1, 'preload must request page-init');
	const beforeInit = preloadTs.slice(0, initAt);
	assert.match(beforeInit, /process\.platform\s*===\s*['"]darwin['"]/);
	assert.match(
		beforeInit,
		/setAttribute\(\s*['"]data-titlebar['"]\s*,\s*['"]overlay['"]\s*\)/
	);
	assert.doesNotMatch(
		beforeInit,
		/if\s*\(\s*init\s*!==\s*null\s*\)[\s\S]*setAttribute\(\s*['"]data-titlebar['"]/
	);
});

type FakeElement = { attributes: Record<string, string>; setAttribute(name: string, value: string): void };
type FakeDocument = {
	documentElement: FakeElement | null;
	listeners: Record<string, () => void>;
	addEventListener(type: string, fn: () => void): void;
};

function fakeElement(): FakeElement {
	const attributes: Record<string, string> = {};
	return { attributes, setAttribute: (name, value) => void (attributes[name] = value) };
}

function fakeDocument(documentElement: FakeElement | null): FakeDocument {
	const listeners: Record<string, () => void> = {};
	return { documentElement, listeners, addEventListener: (type, fn) => void (listeners[type] = fn) };
}

/**
 * Run the compiled preload on a pretend macOS with a stubbed `electron`. As in Electron, the function handed to
 * `executeInMainWorld` runs at once against the page's document, and a throw there comes back into the preload
 * and stops it. Returns whether the preload got as far as its page-init request, which is where the bootstrap
 * page learns its engine origin.
 */
function runPreloadOnMac(document: FakeDocument): { reachedPageInit: boolean } {
	const compiled = fs.readFileSync(path.join(electronRoot, 'dist', 'preload.js'), 'utf8');
	let reachedPageInit = false;
	const electron = {
		contextBridge: {
			executeInMainWorld: (spec: { func: () => void }): void => {
				vm.runInNewContext(`(${String(spec.func)})()`, { document });
			},
			exposeInMainWorld: (): void => {}
		},
		ipcRenderer: {
			sendSync: (channel: string): null => {
				if (channel === 'opendj:page-init') reachedPageInit = true;
				return null;
			},
			invoke: async (): Promise<null> => null,
			on: (): void => {},
			send: (): void => {}
		}
	};
	vm.runInNewContext(compiled, {
		require: (name: string) => {
			assert.equal(name, 'electron', 'a sandboxed preload may only require electron');
			return electron;
		},
		exports: {},
		module: { exports: {} },
		process: { platform: 'darwin' },
		console
	});
	return { reachedPageInit };
}

test('ELECTRON-13662 the preload reaches page-init when the page has no document element yet', () => {
	// A preload runs before the document element exists. An unguarded setAttribute threw there, stopped the
	// preload, and left the bootstrap page on "cannot reach its engine" (set build da92dbd1, Mon 9 Nov 2026;
	// main after #13607, Thu 12 Nov 2026).
	const document = fakeDocument(null);
	let outcome = { reachedPageInit: false };
	assert.doesNotThrow(() => {
		outcome = runPreloadOnMac(document);
	}, 'if the title bar mark throws then the rest of the preload never runs');
	assert.ok(outcome.reachedPageInit, 'if the preload stops before page-init then the page never learns its engine origin');
	assert.ok(document.listeners.DOMContentLoaded, 'if nothing waits for the document then the mark is never set');
	const html = fakeElement();
	document.documentElement = html;
	document.listeners.DOMContentLoaded();
	assert.equal(html.attributes['data-titlebar'], 'overlay');
});

test('ELECTRON-13662 the title bar mark is set at once when the document element already exists', () => {
	const html = fakeElement();
	const { reachedPageInit } = runPreloadOnMac(fakeDocument(html));
	assert.equal(html.attributes['data-titlebar'], 'overlay');
	assert.ok(reachedPageInit);
});

test('LINK-96 top bar uses WebKit drag regions only under the overlay attribute', () => {
	assert.match(topBar, /html\[data-titlebar='overlay'\]\)\s*\.rb-topbar[\s\S]*padding-left:\s*78px/);
	assert.match(
		topBar,
		/html\[data-titlebar='overlay'\]\)[\s\S]*-webkit-app-region:\s*drag/
	);
	assert.match(
		topBar,
		/html\[data-titlebar='overlay'\]\)[\s\S]*-webkit-app-region:\s*no-drag/
	);
});

test('LINK-96 no-drag reaches controls rendered by child components of the top bar', () => {
	// A scoped `.rb-topbar button` only matches TopBar's OWN markup, so a control
	// rendered by a child component inside the drag region would swallow clicks
	// as window drags. The whole selector must be global.
	for (const control of ['button', 'input', 'select', 'textarea', "[role='button']"]) {
		const escaped = control.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
		assert.match(
			topBar,
			new RegExp(`:global\\(html\\[data-titlebar='overlay'\\] \\.rb-topbar ${escaped}\\)`),
			`if ${control} under the overlay top bar is not globally no-drag then child-component controls cannot be clicked`
		);
	}
});

