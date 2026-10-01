/**
 * Assistant sidebar presentation during first-run setup (issue #2590).
 *
 * The panel decision is a pure function, executed here for every input that
 * matters; the component is then rendered for real to prove it consumes that
 * decision and that no key-setup instruction reaches the copy.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let api;
let ssr;

before(async () => {
	api = await loadTypeScriptModule('src/lib/assistant/assistant-api.ts');
	ssr = await loadSvelteSsrModule(
		[
			"export { default as AssistantSidebar } from '$lib/components/assistant/AssistantSidebar.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

test('an unconfigured assistant is hidden from the operator', () => {
	assert.equal(api.assistantPanelMode(true, { configured: false }, null), 'hidden');
});

test('a configured assistant offers the chat', () => {
	assert.equal(api.assistantPanelMode(true, { configured: true }, null), 'chat');
});

test('while the status is still being read nothing flashes up', () => {
	assert.equal(api.assistantPanelMode(true, null, null), 'hidden');
});

test('an unreadable status says so once, without the engine words in the copy', () => {
	assert.equal(api.assistantPanelMode(true, null, 'HTTP 404'), 'unavailable');
	// an error outranks a stale configured reading
	assert.equal(api.assistantPanelMode(true, { configured: true }, 'HTTP 500'), 'unavailable');
});

test('not visible is hidden whatever the status says', () => {
	assert.equal(api.assistantPanelMode(false, { configured: true }, null), 'hidden');
	assert.equal(api.assistantPanelMode(false, null, 'HTTP 500'), 'hidden');
});

test('the rendered sidebar shows nothing before the status has answered', () => {
	const html = ssr.render(ssr.AssistantSidebar, { props: { visible: true } }).body;
	const text = html.replace(/<!--[\s\S]*?-->/g, '').replace(/<[^>]*>/g, '').trim();
	assert.equal(text, '');
});

test('no key-setup instruction is spelled anywhere in the component', () => {
	// The issue's exact leak: "Set OPENROUTER_API_KEY in the engine's
	// environment and restart it." Agents read GET /api/v1/assistant/status.
	const source = readFileSync(
		fileURLToPath(
			new URL('../../src/lib/components/assistant/AssistantSidebar.svelte', import.meta.url)
		),
		'utf8'
	);
	assert.doesNotMatch(source, /OPENROUTER_API_KEY|No API key/);
	assert.match(source, /assistantPanelMode\(visible, status, statusError\)/);
});
