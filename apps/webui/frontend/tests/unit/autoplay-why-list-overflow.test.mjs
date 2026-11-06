import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const STUBS = fileURLToPath(new URL('./autoplay-why-ssr-stubs.mjs', import.meta.url));
const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
const AUTO_PLAY_WHY = read('../../src/lib/components/rb/autoplay/AutoPlayWhy.svelte');
const TOP_BAR = read('../../src/lib/components/rb/TopBar.svelte');

/** Body of the first `selector { ... }` rule in a component's <style>. */
function ruleBody(source, selector) {
	const style = source.slice(source.indexOf('<style'));
	const at = style.indexOf(`${selector} {`);
	assert.notEqual(at, -1, `rule ${selector} not found`);
	return style.slice(at, style.indexOf('}', at));
}

let html = '';

before(async () => {
	const mod = await loadSvelteSsrModule(
		[
			"export { default as AutoPlayWhy } from '$lib/components/rb/autoplay/AutoPlayWhy.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n'),
		{
			alias: {
				'$lib/rb/autoplay-why': STUBS,
				'$lib/rb/performance-ipc.svelte': STUBS,
				'$lib/rb/auto-play-early.svelte': STUBS
			}
		}
	);
	html = mod.render(mod.AutoPlayWhy).body;
});

test('AutoPlayWhy candidate list is classed and testable', () => {
	assert.match(html, /data-testid="ap-why"/);
	assert.match(AUTO_PLAY_WHY, /class="why-list"/);
	assert.match(AUTO_PLAY_WHY, /data-testid="ap-why-candidates"/);
});

test('AutoPlayWhy .why-list scrolls inside a viewport cap', () => {
	const body = ruleBody(AUTO_PLAY_WHY, '.why-list');
	assert.match(body, /max-height:\s*30vh/);
	assert.match(body, /overflow-y:\s*auto/);
	assert.match(body, /margin:\s*0/);
	assert.match(body, /padding-left:\s*1rem/);
	assert.match(body, /font-size:\s*0\.75rem/);
});

test('TopBar still mounts AutoPlayWhy inside the AutoPlay hover card', () => {
	const apMenu = TOP_BAR.indexOf('class="ap-menu"');
	assert.ok(apMenu !== -1, 'expected .ap-menu block');
	const apMenuEnd = TOP_BAR.indexOf('</div>', apMenu);
	assert.ok(apMenuEnd !== -1);
	const block = TOP_BAR.slice(apMenu, apMenuEnd);
	assert.match(block, /<AutoPlayWhy\s*\/>/);
});

test('AutoPlay menu re-places when its fixed node resizes while open', () => {
	assert.match(
		TOP_BAR,
		/\$effect\(\(\) => \{[\s\S]*?if \(!autoPlayMenuOpen\) return;[\s\S]*?const el = autoPlayMenuEl;[\s\S]*?if \(!el\) return;[\s\S]*?const place = \(\): void => \{[\s\S]*?void _placeAutoPlayMenu\(\);[\s\S]*?\};[\s\S]*?place\(\);[\s\S]*?const ro = new ResizeObserver\(place\);[\s\S]*?ro\.observe\(el\);[\s\S]*?window\.addEventListener\('resize', place\);[\s\S]*?return \(\) => \{[\s\S]*?ro\.disconnect\(\);[\s\S]*?window\.removeEventListener\('resize', place\);[\s\S]*?\};[\s\S]*?\}\);/
	);
});
