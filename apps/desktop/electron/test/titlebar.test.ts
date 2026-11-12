import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';

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

