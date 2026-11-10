/**
 * Chat kit lazy loader, citation pills, and streaming bubble (#11169).
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const read = (rel) => readFileSync(fileURLToPath(new URL(`../../${rel}`, import.meta.url)), 'utf8');

const KIT_PATHS = [
	'src/lib/chat-kit/types.ts',
	'src/lib/chat-kit/load.ts',
	'src/lib/components/chat-kit/CitationPill.svelte',
	'src/lib/components/chat-kit/StreamingBubble.svelte',
	'src/lib/components/chat-kit/CitationPill.stories.ts',
	'src/lib/components/chat-kit/StreamingBubble.stories.ts'
];

const staticImportOf = (file) => new RegExp(`import\\s+\\w+\\s+from\\s+['"][^'"]*${file}['"]`);
const dynamicImportOf = (file) => new RegExp(`import\\(\\s*['"][^'"]*${file}['"]\\s*\\)`);

let types;
let load;
let ssr;

before(async () => {
	types = await loadTypeScriptModule('src/lib/chat-kit/types.ts');
	load = await loadTypeScriptModule('src/lib/chat-kit/load.ts');
	ssr = await loadSvelteSsrModule(
		[
			"export { default as CitationPill } from '$lib/components/chat-kit/CitationPill.svelte';",
			"export { default as StreamingBubble } from '$lib/components/chat-kit/StreamingBubble.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

const renderPill = (state) =>
	ssr.render(ssr.CitationPill, {
		props: {
			citation: { label: 'source', state, href: state === 'fail' ? null : 'https://example.test' }
		}
	}).body;

test('[if] CitationPill is given state verified, warn, or fail [then] it renders data-state and theme classes', () => {
	for (const state of types.CITATION_STATES) {
		const body = renderPill(state);
		assert.match(body, new RegExp(`data-state="${state}"`));
		assert.match(body, new RegExp(`class="[^"]*\\b${state}\\b`));
	}
});

test('[if] StreamingBubble is fed partial token deltas [then] settled text and delta show together while streaming', () => {
	assert.equal(types.appendTextDelta('Hello', ' world'), 'Hello world');
	const body = ssr.render(ssr.StreamingBubble, {
		props: { text: 'Deck 1 is ', delta: 'playing.', streaming: true }
	}).body;
	assert.match(body, /Deck 1 is playing\./);
	assert.match(body, /data-streaming="true"/);
	assert.match(body, /class="cursor\b/);
});

test('[if] a unit test static-imports only $lib/chat-kit/load [then] components load via import()', async () => {
	const loader = read('src/lib/chat-kit/load.ts');
	assert.doesNotMatch(loader, staticImportOf('CitationPill\\.svelte'));
	assert.doesNotMatch(loader, staticImportOf('StreamingBubble\\.svelte'));
	assert.match(loader, dynamicImportOf('CitationPill\\.svelte'));
	assert.match(loader, dynamicImportOf('StreamingBubble\\.svelte'));
	load.resetChatKitLoadForTests();
	const kit = await load.loadChatKit();
	assert.equal(typeof kit.CitationPill, 'function');
	assert.equal(typeof kit.StreamingBubble, 'function');
});

test('[if] apps/webui/frontend/src/routes/performance/+page.svelte is parsed [then] it never static-imports chat-kit', () => {
	const page = read('src/routes/performance/+page.svelte');
	assert.doesNotMatch(page, /chat-kit/);
});

test('[if] a kit file imports react or a React chat component [then] stop', () => {
	const reactImport = /\bfrom\s+['"]react(?:-dom)?['"]|\bimport\s+react\b/i;
	for (const rel of KIT_PATHS) {
		const src = read(rel);
		assert.doesNotMatch(src, reactImport, rel);
	}
});

test('app.css declares chat-kit citation tokens on :root', () => {
	const css = read('src/app.css');
	for (const token of [
		'--chat-citation-verified-bg',
		'--chat-citation-warn-bg',
		'--chat-citation-fail-bg',
		'--chat-bubble-cursor'
	]) {
		assert.match(css, new RegExp(`${token}:`));
	}
});
