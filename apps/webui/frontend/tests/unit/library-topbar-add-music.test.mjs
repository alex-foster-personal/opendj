/**
 * SETUP-8732 (issue #2457 / FLOW-15): persistent Add music in Library chrome.
 */
// requirement: SETUP-8732
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const CONTAINER = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/library/LibraryTopBar.svelte', import.meta.url)),
	'utf8'
);

const ENTRY = [
	"export { default as View } from '$lib/components/library/LibraryTopBarView.svelte';",
	"export { APP_MODES, appModeForPath } from '$lib/rb/app-mode';",
	"export { SIDEBAR_NAV_LINKS } from '$lib/shell/sidebar-nav';",
	"export { createRawSnippet } from 'svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

let ssr;

before(async () => {
	ssr = await loadSvelteSsrModule(ENTRY);
});

function renderBar(libraryCounts) {
	const cloudSyncChip = ssr.createRawSnippet(() => ({
		render: () => '<span>sync</span>'
	}));
	return ssr.render(ssr.View, {
		props: {
			activeMode: ssr.appModeForPath('/'),
			pageLinks: ssr.SIDEBAR_NAV_LINKS.filter((link) => !link.dev),
			pathname: '/',
			selectedCount: 0,
			libraryCounts,
			selectMode: () => {},
			modeForHref: (href) => ssr.APP_MODES.find((mode) => mode.href === href),
			addMusicLabel: 'Add music',
			addMusicTitle: 'Open the library import wizard.',
			addMusicBlocked: null,
			onAddMusic: () => {},
			cloudSyncChip
		}
	}).body;
}

function addMusicButton(html) {
	const match = html.match(/<button\b[^>]*data-testid="library-add-music"[^>]*>/);
	assert.ok(match, 'library-add-music button must render');
	return match[0];
}

test('SETUP-8732: LibraryTopBar wires runSetup through the shared module', () => {
	assert.match(CONTAINER, /from '\$lib\/setup\/run-setup'/);
	assert.match(CONTAINER, /runSetup\(goto, \$page\.url\.pathname\)/);
	assert.match(CONTAINER, /ADD_MUSIC_LABEL/);
	assert.doesNotMatch(CONTAINER, /IMPORT_MUSIC_LABEL/);
	assert.doesNotMatch(CONTAINER, /RUN_SETUP_LABEL/);
	assert.match(CONTAINER, /pushToast/);
	assert.doesNotMatch(CONTAINER, /goto\(\s*['"]\/setup['"]\s*\)/);
	assert.doesNotMatch(CONTAINER, /setupWizard\.reopen\(\)/);
});

test('SETUP-8732: zero tracks shows a live prominent Add music button', () => {
	const html = renderBar({ tracks: 0, playlists: 0 });
	const button = addMusicButton(html);
	assert.match(button, /\bprominent\b/);
	assert.doesNotMatch(button, /\bdisabled\b/);
	assert.match(html, />Add music</);
	assert.match(html, /data-testid="library-quick-select"/);
});

test('SETUP-8732: a populated library still shows Add music, not prominent', () => {
	const html = renderBar({ tracks: 8355, playlists: 214 });
	const button = addMusicButton(html);
	assert.doesNotMatch(button, /\bprominent\b/);
});

test('SETUP-8732: counts pending still shows Add music, not prominent', () => {
	const html = renderBar(null);
	const button = addMusicButton(html);
	assert.doesNotMatch(button, /\bprominent\b/);
});
