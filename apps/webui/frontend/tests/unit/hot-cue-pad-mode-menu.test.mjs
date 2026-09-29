/**
 * requirement: CHROME-11
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { resolve } from 'node:path';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const hotCueBank = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/deck/HotCueBank.svelte', import.meta.url)),
	'utf8'
);
// The dropdown lives in its own component so HotCueBank stays under the
// 600-line review threshold; HotCueBank renders it once per deck.
const padModeMenu = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/deck/PadModeMenu.svelte', import.meta.url)),
	'utf8'
);

let vite;
let catalog;

test.before(async () => {
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true },
		plugins: [svelte()],
		resolve: {
			alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') },
			conditions: ['browser']
		}
	});
	catalog = await vite.ssrLoadModule('/src/lib/rb/pad-mode-catalog.ts');
});

test.after(async () => {
	await vite.close();
});

test('the hot cue bank renders the pad-mode menu for its own deck', () => {
	const markup = hotCueBank.slice(hotCueBank.lastIndexOf('</script>'));
	assert.match(markup, /<PadModeMenu deckId=\{deck\.deck_id\} \/>/);
});

test('hot cue menu lists every catalog pad mode', () => {
	const markup = padModeMenu.slice(padModeMenu.lastIndexOf('</script>'));
	assert.match(markup, /PAD_MODE_CATALOG/);
	assert.match(markup, /padModeMenuLabel\(entry\)/);
	assert.equal(catalog.PAD_MODE_CATALOG.length, 8);
});

test('unbuilt pad modes are marked not-built-yet', () => {
	const unbuilt = catalog.PAD_MODE_CATALOG.filter((e) => !e.built);
	assert.ok(unbuilt.length > 0);
	for (const entry of unbuilt) {
		assert.equal(catalog.padModeMenuLabel(entry).includes('not-built-yet'), true);
	}
});

test('hot cue menu button is enabled with data-testid preserved', () => {
	const open = padModeMenu.lastIndexOf('<button', padModeMenu.indexOf('data-testid={`hot-cue-menu-deck-${deckId}`}'));
	const menuButton = padModeMenu.slice(open, padModeMenu.indexOf('</button>', open));
	assert.match(menuButton, /data-testid=\{`hot-cue-menu-deck-\$\{deckId\}`\}/);
	assert.match(menuButton, /aria-label=\{`hot cue menu deck \$\{deckId\}`\}/);
	assert.match(menuButton, /title="Pad mode menu/, 'the live dropdown needs its own explainer');
	assert.doesNotMatch(menuButton, /\bdisabled\b/);
});

test('unbuilt pad-mode items render inert with a tooltip, built ones stay live', () => {
	const item = padModeMenu.slice(padModeMenu.indexOf('class="pad-menu-item"'));
	assert.match(item, /disabled=\{!entry\.built\}/);
	assert.match(item, /title=\{entry\.built \? undefined : NOT_BUILT_TIP\}/);
	assert.match(padModeMenu, /const NOT_BUILT_TIP = 'not implemented - see PARITY-TODO'/);
});

// The deck and its main row set overflow: hidden (Deck.svelte), so an
// absolutely positioned menu below the bank was clipped. The menu must be
// fixed and placed from the trigger rect through the shared placeFloating
// path.
test('pad-mode menu is fixed-positioned via triggerFloatingAction, not absolute inside the deck', () => {
	assert.match(padModeMenu, /import \{ triggerFloatingAction \} from '\$lib\/ui\/clamp-to-viewport'/);
	const menuOpen = padModeMenu.indexOf('class="pad-menu"');
	const menuTag = padModeMenu.slice(padModeMenu.lastIndexOf('<div', menuOpen), padModeMenu.indexOf('>', padModeMenu.indexOf('}}', menuOpen)) + 1);
	assert.match(menuTag, /use:triggerFloatingAction=\{\{ getTrigger: \(\) => triggerEl \?\? null, preferred: 'below', gap: 4 \}\}/);
	const css = padModeMenu.slice(padModeMenu.indexOf('<style>'));
	const rule = css.slice(css.indexOf('.pad-menu {'), css.indexOf('}', css.indexOf('.pad-menu {')));
	assert.match(rule, /position: fixed;/);
	assert.doesNotMatch(rule, /position: absolute/);
	assert.doesNotMatch(rule, /top: calc\(100%/);
});

test('pad-mode menu closes on outside pointerdown and on Escape', () => {
	assert.match(padModeMenu, /<svelte:window onpointerdown=\{onWindowPointerDown\} onkeydown=\{onWindowKeyDown\} \/>/);
	const down = padModeMenu.slice(padModeMenu.indexOf('function onWindowPointerDown'), padModeMenu.indexOf('function onWindowKeyDown'));
	assert.match(down, /wrapEl\?\.contains\(target\)\) return;/);
	assert.match(down, /open = false;/);
	const key = padModeMenu.slice(padModeMenu.indexOf('function onWindowKeyDown'), padModeMenu.indexOf('</script>'));
	assert.match(key, /e\.key !== 'Escape'/);
	assert.match(key, /open = false;/);
});

test('triggerFloatingAction places the 8-item menu fixed and inside the viewport for a deck-bottom trigger', async () => {
	const clamp = await vite.ssrLoadModule('/src/lib/ui/clamp-to-viewport.ts');
	const saved = {
		window: globalThis.window,
		ResizeObserver: globalThis.ResizeObserver,
		requestAnimationFrame: globalThis.requestAnimationFrame
	};
	const viewport = { width: 1280, height: 800 };
	globalThis.window = {
		innerWidth: viewport.width,
		innerHeight: viewport.height,
		addEventListener() {},
		removeEventListener() {}
	};
	globalThis.ResizeObserver = class {
		observe() {}
		disconnect() {}
	};
	const frames = [];
	globalThis.requestAnimationFrame = (cb) => frames.push(cb);
	try {
		// HOT CUE button at the bottom of a ~248px deck that sits at the
		// bottom of the window: an 8-item menu (~190px) cannot open below.
		const triggerRect = { left: 40, top: 770, width: 70, height: 18 };
		const trigger = { getBoundingClientRect: () => triggerRect };
		const size = { width: 180, height: 8 * 23 + 8 };
		const node = { offsetWidth: size.width, offsetHeight: size.height, style: {} };
		const action = clamp.triggerFloatingAction(node, { getTrigger: () => trigger, preferred: 'below', gap: 4 });
		assert.equal(frames.length, 1);
		frames[0]();
		assert.equal(node.style.position, 'fixed');
		const expected = clamp.placeFloating({ trigger: triggerRect, size, viewport, preferred: 'below', gap: 4 });
		assert.equal(node.style.left, `${Math.round(expected.x)}px`);
		assert.equal(node.style.top, `${Math.round(expected.y)}px`);
		const top = parseInt(node.style.top, 10);
		assert.equal(top, triggerRect.top - size.height - 4, 'flips above the trigger when below overflows');
		assert.ok(top >= clamp.VIEWPORT_MARGIN_PX && top + size.height <= viewport.height - clamp.VIEWPORT_MARGIN_PX);
		action.destroy();
	} finally {
		Object.assign(globalThis, saved);
	}
});
