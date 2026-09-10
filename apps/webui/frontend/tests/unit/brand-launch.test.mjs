/**
 * OPS-10 brand launch contract.
 *
 * - if the launch marker is absent then the launch animation plays once
 * - if a completed marker is present then a later launch is immediately usable
 * - if the shipped mark stops being the two-shade broken circle then the icon
 *   the maintainer reverted to on Wed 9 Sep 2026 has been replaced again
 * - if the app shell stops showing the plain "Open DJ" wordmark then the
 *   reverted identity no longer matches that mark
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let brand;

before(async () => {
	brand = await loadTypeScriptModule('src/lib/brand-launch.ts');
});

test('a new installation plays the launch exactly once', () => {
	const values = new Map();
	const storage = {
		getItem: (key) => values.get(key) ?? null,
		setItem: (key, value) => values.set(key, value)
	};

	assert.equal(brand.shouldPlayBrandLaunch(storage), true);
	brand.completeBrandLaunch(storage);
	assert.equal(brand.shouldPlayBrandLaunch(storage), false);
});

test('only the exact completion marker suppresses the launch', () => {
	const storage = { getItem: () => 'partial', setItem: () => undefined };
	assert.equal(brand.shouldPlayBrandLaunch(storage), true);
});

test('the launch wordmark is Anybody 800 wdth 150 in caps, shipped offline with its licence', () => {
	const root = fileURLToPath(new URL('../..', import.meta.url));
	const launch = readFileSync(`${root}/src/lib/components/BrandLaunch.svelte`, 'utf8');
	const woff2 = readFileSync(`${root}/static/fonts/anybody-800-w150-wordmark.woff2`);

	// if the face, file or caps treatment drifts then the maintainer's Thu 10 Sep 2026 pick is gone
	assert.match(launch, /url\('\/fonts\/anybody-800-w150-wordmark\.woff2'\)/);
	assert.match(launch, /\.brand-name \{[^}]*font-family: 'Anybody Wordmark'/);
	assert.match(launch, /\.brand-name \{[^}]*text-transform: uppercase/);
	assert.equal(woff2.subarray(0, 4).toString('latin1'), 'wOF2');
	// if the licence stops shipping beside the font then the OFL terms are broken
	assert.match(readFileSync(`${root}/static/fonts/Anybody-OFL.txt`, 'utf8'), /SIL Open Font License, Version 1\.1/);
});

test('the desktop engine-startup headline matches the launch wordmark, from its own directory', () => {
	const root = fileURLToPath(new URL('../../../../desktop/setup/', import.meta.url));
	const css = readFileSync(`${root}setup.css`, 'utf8');

	// if the startup page drifts from the launch face then the two first screens disagree
	assert.match(css, /url\('\.\/anybody-800-w150-headline\.woff2'\)/);
	assert.match(css, /\nh1 \{[^}]*font-family: 'Anybody Wordmark'[^}]*system-ui/);
	assert.match(css, /\nh1 \{[^}]*text-transform: uppercase/);
	// if the font leaves the page's own directory then the offline-first page needs the network
	assert.equal(readFileSync(`${root}anybody-800-w150-headline.woff2`).subarray(0, 4).toString('latin1'), 'wOF2');
	assert.match(readFileSync(`${root}Anybody-OFL.txt`, 'utf8'), /SIL Open Font License, Version 1\.1/);
	assert.doesNotMatch(css, /https?:\/\//);
});

test('the app shell mounts a non-blocking launch and the Open DJ wordmark', () => {
	const root = fileURLToPath(new URL('../..', import.meta.url));
	const layout = readFileSync(`${root}/src/routes/+layout.svelte`, 'utf8');
	const launch = readFileSync(`${root}/src/lib/components/BrandLaunch.svelte`, 'utf8');

	const browserPanel = readFileSync(`${root}/src/lib/components/rb/BrowserPanel.svelte`, 'utf8');

	assert.match(layout, /<BrandLaunch \/>/);
	assert.match(layout, /<h1>Open DJ<\/h1>/);
	// The toolbar wordmark has read lowercase since long before the oDj mark,
	// so the revert restores "open dj" here and "Open DJ" in the sidebar. Both
	// are pinned, or a later tidy-up "fixes" one of them into drift.
	assert.match(browserPanel, /<span class="wordmark">open dj<\/span>/);
	assert.match(launch, /pointer-events:\s*none/);
	assert.match(launch, /animationend/);
	assert.match(launch, />Open DJ</);
	// The launch must settle on the SHIPPED mark, not a hand-drawn imitation of
	// it, or the artwork and the animation can drift apart again.
	assert.match(launch, /url\('\/favicon\.svg'\)/);

	// An INVARIANT, not a pinned value: the mark's two halves carry different
	// shades, so the spin must end on a whole number of turns or the launch
	// settles on a mark whose colors are swapped against the shipped artwork.
	// 900deg shipped in #1367 and was 180deg out; nothing caught it, because
	// the rings it drew then were their own artwork and could not disagree
	// with anything.
	const finalRotation = launch.match(/100%\s*\{\s*transform:\s*rotate\((-?\d+(?:\.\d+)?)deg\)/);
	assert.ok(finalRotation, 'the launch spin must declare a terminal rotation');
	assert.equal(Number(finalRotation[1]) % 360, 0);
});

test('the shipped mark is the two-shade broken circle the maintainer reverted to', () => {
	const root = fileURLToPath(new URL('../..', import.meta.url));
	const mark = readFileSync(`${root}/static/favicon.svg`, 'utf8');

	// Both half-discs must be present: one shade alone is half a logo.
	assert.match(mark, /fill="#D97757"/);
	assert.match(mark, /fill="#9c4b34"/);
	assert.match(mark, /A 188\.00,188\.00 /);
});
