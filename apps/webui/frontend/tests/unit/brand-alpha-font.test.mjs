/**
 * OSSPUB-07 (the maintainer, Mon 5 Oct 2026): a quiet alpha badge and the Open DJ brand font.
 *
 * - if the alpha badge keeps a border, a background or a hard-coded colour,
 *   or is not lowercase italic in the theme's text colour, then broken
 * - if any surface renders its own alpha label instead of the shared
 *   AlphaBadge component, then the style drifts: broken
 * - if the bottom tray still shows the badge after "open dj", then broken
 * - if a brand surface does not reference --rb-font-brand, or the variable
 *   does not name the shipped Anybody face, then broken
 * - if the brand font leaks onto the track list or data tables, then
 *   tabular numerals lose legibility: broken
 */
import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));
const SRC = join(FRONTEND, 'src');
const read = (rel) => readFileSync(join(SRC, rel), 'utf8');

/** The CSS block for `selector` inside a component's <style>. */
function cssRule(source, selector) {
	const style = source.match(/<style[^>]*>([\s\S]*?)<\/style>/)?.[1] ?? source;
	const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
	const match = style.match(new RegExp(`(?:^|\\n)\\s*${escaped}\\s*\\{([^}]*)\\}`));
	assert.ok(match, `no CSS rule for ${selector}`);
	return match[1];
}

function svelteFiles(dir) {
	return readdirSync(dir).flatMap((name) => {
		const path = join(dir, name);
		if (statSync(path).isDirectory()) return svelteFiles(path);
		return name.endsWith('.svelte') ? [path] : [];
	});
}

const BRAND_SURFACES = [
	['lib/components/preflight/PreflightScreen.svelte', '.preflight-name'],
	['lib/components/rb/BrowserPanel.svelte', '.wordmark'],
	['lib/components/rb/EditSuiteModal.svelte', '.es-header h2'],
	['lib/components/rb/MidiPanel.svelte', '.drawer-title'],
	['lib/components/rb/FeedbackPanel.svelte', '.fb-panel-title'],
	['lib/components/rb/IngestDropModal.svelte', '.m-title'],
	['lib/components/rb/PlaylistFolderDupModal.svelte', '.m-title']
];

test('OSSPUB-07 if the alpha badge has a border, fill or fixed colour, or is not lowercase italic, then broken', () => {
	const rule = cssRule(read('lib/components/AlphaBadge.svelte'), '.release-stage');
	assert.match(rule, /border:\s*none;/);
	assert.match(rule, /background:\s*none;/);
	assert.match(rule, /font-style:\s*italic;/);
	assert.match(rule, /text-transform:\s*lowercase;/);
	assert.match(rule, /color:\s*var\(--rb-text,\s*var\(--fg\)\);/);
	assert.doesNotMatch(rule, /#[0-9a-f]{3,8}\b|uppercase|border-(?:color|width|style)|border:\s*\d/i);
});

test('OSSPUB-07 if a surface renders its own alpha label instead of AlphaBadge then broken', () => {
	const users = svelteFiles(SRC).filter((path) => /<AlphaBadge\s*\/>/.test(readFileSync(path, 'utf8')));
	assert.ok(users.length >= 1, 'no AlphaBadge usage found: the probe cannot see the badge');
	for (const path of svelteFiles(SRC)) {
		if (path.endsWith('AlphaBadge.svelte')) continue;
		const markup = readFileSync(path, 'utf8').replace(/<style[\s\S]*?<\/style>|<script[\s\S]*?<\/script>|<!--[\s\S]*?-->/g, '');
		assert.doesNotMatch(markup, />\s*alpha\s*</i, `${path} renders its own alpha label`);
		assert.doesNotMatch(markup, /release-stage-badge/, `${path} copies the badge markup`);
	}
});

test('OSSPUB-07 if the bottom tray still shows the alpha badge then broken', () => {
	const panel = read('lib/components/rb/BrowserPanel.svelte');
	assert.match(panel, /<span class="wordmark">open dj<\/span>/);
	assert.doesNotMatch(panel, /AlphaBadge/);
	const tray = panel.match(/<div class="bottom-bar">[\s\S]*?<LibraryJobsChrome/)?.[0];
	assert.ok(tray, 'bottom-bar markup not found');
	assert.doesNotMatch(tray, /alpha|release-stage/i);
});

test('OSSPUB-07 if the welcome screen loses its badge then broken', () => {
	assert.match(read('lib/components/preflight/PreflightScreen.svelte'), /<span class="preflight-name">Open DJ<AlphaBadge \/><\/span>/);
});

test('OSSPUB-07 if --rb-font-brand does not name the shipped, licensed Anybody face then broken', () => {
	const css = read('app.css');
	const face = css.match(/@font-face\s*\{([^}]*)\}/)?.[1] ?? '';
	assert.match(face, /font-family:\s*"Anybody"/);
	const url = face.match(/url\("\/(fonts\/[^"]+\.woff2)"\)/)?.[1];
	assert.ok(url, 'no woff2 url in the @font-face');
	assert.ok(existsSync(join(FRONTEND, 'static', url)), `${url} is not shipped in static/`);
	assert.match(readFileSync(join(FRONTEND, 'static/fonts/Anybody-OFL.txt'), 'utf8'), /SIL Open Font License, Version 1\.1/);
	assert.match(css, /:root\s*\{[^}]*--rb-font-brand:\s*"Anybody",/);
});

for (const [file, selector] of BRAND_SURFACES) {
	test(`OSSPUB-07 if ${selector} in ${file.split('/').pop()} does not use the brand font then broken`, () => {
		assert.match(cssRule(read(file), selector), /font-family:\s*var\(--rb-font-brand\);/);
	});
}

test('OSSPUB-07 if the brand font leaks onto the track list or tables then broken', () => {
	const brandFiles = new Set(BRAND_SURFACES.map(([file]) => join(SRC, file)));
	brandFiles.add(join(SRC, 'app.css'));
	const leaks = svelteFiles(SRC)
		.filter((path) => !brandFiles.has(path) && readFileSync(path, 'utf8').includes('--rb-font-brand'))
		.map((path) => path.slice(SRC.length + 1));
	assert.deepEqual(leaks, [], 'only the listed brand surfaces may use --rb-font-brand');
	assert.equal(read('lib/components/rb/BrowserPanel.svelte').match(/--rb-font-brand/g)?.length, 1);
});
