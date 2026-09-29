/**
 * LIBM-92: ContextMenu unavailable items use aria-disabled and stay focusable.
 *
 * WHAT A PASS HERE DOES NOT COVER: no DOM (no jsdom/happy-dom), so this proves
 * nothing about layout, stacking, or real `.focus()`. Absence of the HTML
 * `disabled` attribute is the condition for staying in the tab / arrow-key order.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const MENU_PATH = `${SRC}/lib/components/rb/ContextMenu.svelte`;

const ENTRY = [
	"export { default as ContextMenu } from '$lib/components/rb/ContextMenu.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

let mod;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
});

function renderMenu(items) {
	return mod.render(mod.ContextMenu, {
		props: {
			items,
			x: 10,
			y: 20,
			onclose: () => {}
		}
	}).body;
}

function menuItemButtons(html) {
	return [...html.matchAll(/<button[^>]*role="menuitem"[^>]*>/g)].map((match) => match[0]);
}

function hasHtmlDisabledAttr(tag) {
	return /\sdisabled(?:\s*=|(?=[\s/>]))/.test(tag);
}

test('SSR: available item has no aria-disabled, describedby, or description span', () => {
	const html = renderMenu([
		{ id: 'go', label: 'Go', run: () => {} }
	]);
	const buttons = menuItemButtons(html);
	assert.equal(buttons.length, 1);
	assert.equal(buttons[0].includes('aria-disabled="true"'), false);
	assert.equal(buttons[0].includes('aria-describedby'), false);
	assert.equal(hasHtmlDisabledAttr(buttons[0]), false);
	assert.equal(html.includes('visually-hidden'), false);
});

test('SSR: unavailable item without title uses aria-disabled and PARITY-TODO description', () => {
	const html = renderMenu([
		{ id: 'todo', label: 'Mark offline' }
	]);
	const buttons = menuItemButtons(html);
	assert.equal(buttons.length, 1);
	assert.match(buttons[0], /aria-disabled="true"/);
	assert.equal(hasHtmlDisabledAttr(buttons[0]), false);
	assert.match(buttons[0], /aria-describedby="ctx-menu-desc-todo"/);
	assert.match(buttons[0], /title="not implemented - see PARITY-TODO"/);
	assert.match(html, /id="ctx-menu-desc-todo"[^>]*class="visually-hidden[^"]*"[^>]*>not implemented - see PARITY-TODO<\/span>/);
});

test('SSR: unavailable item with custom title uses that text for title and description', () => {
	const html = renderMenu([
		{ id: 'why', label: 'Bulk edit', title: 'select at least one track first' }
	]);
	const buttons = menuItemButtons(html);
	assert.equal(buttons.length, 1);
	assert.match(buttons[0], /aria-disabled="true"/);
	assert.equal(hasHtmlDisabledAttr(buttons[0]), false);
	assert.match(buttons[0], /aria-describedby="ctx-menu-desc-why"/);
	assert.match(buttons[0], /title="select at least one track first"/);
	assert.match(html, /id="ctx-menu-desc-why"[^>]*class="visually-hidden[^"]*"[^>]*>select at least one track first<\/span>/);
});

test('SSR: mixed menu has no HTML disabled on any menuitem', () => {
	const html = renderMenu([
		{ id: 'go', label: 'Go', run: () => {} },
		{ id: 'todo', label: 'Mark offline' },
		{ id: 'why', label: 'Bulk edit', title: 'select at least one track first' }
	]);
	const buttons = menuItemButtons(html);
	assert.equal(buttons.length, 3);
	for (const button of buttons) {
		assert.equal(hasHtmlDisabledAttr(button), false, `unexpected disabled attr: ${button}`);
	}
});

test('source: activate no-ops before onclose when run is missing', () => {
	const source = readFileSync(MENU_PATH, 'utf8');
	assert.match(source, /if \(item\.run === undefined\) return;/);
	const activateBlock = source.slice(source.indexOf('async function activate'));
	const beforeOnclose = activateBlock.slice(0, activateBlock.indexOf('onclose()'));
	assert.match(beforeOnclose, /item\.run === undefined/);
});

test('source: onclick calls activate and Enter/Space on aria-disabled are swallowed', () => {
	const source = readFileSync(MENU_PATH, 'utf8');
	assert.match(source, /onclick=\{\(\) => void activate\(item\)\}/);
	assert.match(source, /getAttribute\('aria-disabled'\) === 'true'/);
	assert.match(source, /event\.key === 'Enter' \|\| event\.key === ' '/);
	assert.match(source, /event\.preventDefault\(\)/);
});

// REQ: DECKUX-18
test('source: arrow keys walk all menuitems without skipping aria-disabled', () => {
	const source = readFileSync(MENU_PATH, 'utf8');
	// #2416 (08b04b55e) added checkable rows; the walk must cover both roles
	// and still carry no aria-disabled / :disabled exclusion (asserted below).
	assert.match(
		source,
		/querySelectorAll<HTMLElement>\('\[role="menuitem"\], \[role="menuitemcheckbox"\]'\)/
	);
	assert.equal(source.includes(':not([aria-disabled])'), false);
	assert.equal(source.includes(':not(:disabled)'), false);
	assert.match(source, /event\.key === 'ArrowDown'/);
	assert.match(source, /event\.key === 'ArrowUp'/);
	assert.match(source, /event\.key === 'Home'/);
	assert.match(source, /event\.key === 'End'/);
});

// REQ: DECKUX-18
test('source: arrow keys are ignored after focus leaves the menu', () => {
	const source = readFileSync(MENU_PATH, 'utf8');
	const keydown = source.slice(source.indexOf('function onKeydown'));
	const itemLookup = keydown.indexOf('const items = menuItems();');
	assert.notEqual(itemLookup, -1);
	const beforeItemLookup = keydown.slice(0, itemLookup);
	assert.match(beforeItemLookup, /menu === null/);
	assert.match(beforeItemLookup, /!menu\.contains\(document\.activeElement\)/);
});

test('source: CSS dims via aria-disabled, not button:disabled', () => {
	const source = readFileSync(MENU_PATH, 'utf8');
	assert.match(source, /button\[aria-disabled='true'\]/);
	assert.equal(source.includes('button:disabled'), false);
});
