/**
 * Issue #2339: failed deck loads publish `decks.N.load_error` and keep error
 * toasts long enough for AGT `read_page`.
 *
 * Regression lines:
 *   - if buildUiMirror omits load_error then broken
 *   - if a pre-dispatch refusal does not call recordDeckLoadRefusal when deck is set then broken
 *   - if load-error toasts use the 5 s default then broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

function source(relativePath) {
	return readFileSync(fileURLToPath(new URL(`../../${relativePath}`, import.meta.url)), 'utf8');
}

let ipc;
let deckLoadContext;

before(async () => {
	const opts = { viteApiBase: 'https://ui-mirror-deck-load-error.example.test' };
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts', opts);
	deckLoadContext = await loadTypeScriptModule('src/lib/rb/deck-load-context.ts', opts);
});

test('buildUiMirror publishes load_error from deckLoadErrors', () => {
	const mirror = source('src/lib/rb/ui-mirror.ts');
	assert.match(mirror, /import \{ deckLoadErrors \} from '\$lib\/player\/state\.svelte';/);
	const build = mirror.slice(
		mirror.indexOf('export function buildUiMirror'),
		mirror.indexOf('export function installUiMirror')
	);
	assert.match(build, /load_error: deckLoadErrors\[Number\(id\) as DeckId\] \?\? null/);
});

test('deck load failure toasts use the 8 s constant', () => {
	const ctx = source('src/lib/rb/deck-load-context.ts');
	assert.match(ctx, /export const DECK_LOAD_ERROR_TOAST_MS = 8000;/);
	assert.match(ctx, /DECK_LOAD_ERROR_TOAST_MS,\s*\n\t\tcause,/);
	assert.equal(deckLoadContext.DECK_LOAD_ERROR_TOAST_MS, 8000);
});

test('BrowserPanel _loadOntoDeck records refusals on a concrete deck', () => {
	const panel = source('src/lib/components/rb/BrowserPanel.svelte');
	const loadFn = panel.slice(panel.indexOf('async function _loadOntoDeck'), panel.indexOf('export function commandBarRows'));
	assert.match(loadFn, /if \(loadRefusal !== null\)/);
	assert.match(loadFn, /if \(deck !== null\) recordDeckLoadRefusal\(deck, loadRefusal\)/);
	assert.match(loadFn, /pushToast\(loadRefusal, 'error', DECK_LOAD_ERROR_TOAST_MS\)/);
	assert.match(loadFn, /recordDeckLoadRefusal\(\s*\n\t\t\t\t\ttarget,/);
});

test('recordDeckLoadRefusal stamps deck errors and uses the 8 s toast constant', () => {
	const msg = 'cannot load: audio file missing on disk (broken link)';
	const ipcSource = source('src/lib/rb/performance-ipc.svelte.ts');
	const helper = ipcSource.slice(
		ipcSource.indexOf('export function recordDeckLoadRefusal'),
		ipcSource.indexOf('function _persistCommandError')
	);
	assert.match(helper, /pushToast\(message, 'error', DECK_LOAD_ERROR_TOAST_MS\)/);
	ipc.performanceCommandStatus.deck_errors[1] = null;
	ipc.recordDeckLoadRefusal(1, msg);
	assert.equal(ipc.performanceCommandStatus.deck_errors[1], msg);
});
