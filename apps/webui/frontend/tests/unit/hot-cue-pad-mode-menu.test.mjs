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

test('hot cue menu lists every catalog pad mode', () => {
	const markup = hotCueBank.slice(hotCueBank.lastIndexOf('</script>'));
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
	const menuButton = hotCueBank.slice(
		hotCueBank.indexOf('data-testid={`hot-cue-menu-deck-${deck.deck_id}`}'),
		hotCueBank.indexOf('</button>', hotCueBank.indexOf('data-testid={`hot-cue-menu-deck-${deck.deck_id}`}'))
	);
	assert.match(menuButton, /data-testid=\{`hot-cue-menu-deck-\$\{deck\.deck_id\}`\}/);
	assert.doesNotMatch(menuButton, /\bdisabled\b/);
});
