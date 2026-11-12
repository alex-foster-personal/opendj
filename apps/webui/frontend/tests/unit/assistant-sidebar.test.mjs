/**
 * Assistant sidebar presentation during first-run setup (issue #2590, OJ branding #9473).
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
let present;
let ssr;

const viewSourcePath = fileURLToPath(
	new URL('../../src/lib/components/assistant/AssistantSidebarView.svelte', import.meta.url)
);
const containerSourcePath = fileURLToPath(
	new URL('../../src/lib/components/assistant/AssistantSidebar.svelte', import.meta.url)
);

before(async () => {
	api = await loadTypeScriptModule('src/lib/assistant/assistant-api.ts');
	present = await loadTypeScriptModule('src/lib/setup/present.ts');
	ssr = await loadSvelteSsrModule(
		[
			"export { default as AssistantSidebar } from '$lib/components/assistant/AssistantSidebar.svelte';",
			"export { default as AssistantSidebarView } from '$lib/components/assistant/AssistantSidebarView.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

const viewProps = (over = {}) => ({
	visible: true,
	mode: 'chat',
	status: { configured: true, model: 'example/model-small' },
	statusError: null,
	messages: [],
	streaming: null,
	sendError: null,
	canSend: false,
	draft: '',
	send: async () => {},
	onStop: () => {},
	...over
});

const renderView = (over) => ssr.render(ssr.AssistantSidebarView, { props: viewProps(over) }).body;

const textOf = (html) => html.replace(/<!--[\s\S]*?-->/g, '').replace(/<[^>]*>/g, ' ');

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
	const text = html
		.replace(/<!--[\s\S]*?-->/g, '')
		.replace(/<[^>]*>/g, '')
		.trim();
	assert.equal(text, '');
});

test('chat header mounts OJ at 28px when idle', () => {
	const html = ssr.render(ssr.AssistantSidebarView, {
		props: {
			visible: true,
			mode: 'chat',
			status: { configured: true },
			statusError: null,
			messages: [],
			streaming: null,
			sendError: null,
			canSend: true,
			draft: '',
			send: async () => {},
			onStop: () => {}
		}
	}).body;
	assert.match(html, /<header[\s\S]*aria-label="OJ"/);
	assert.match(html, /width="28"/);
});

test('chat header shows think with bubbles while streaming', () => {
	const html = ssr.render(ssr.AssistantSidebarView, {
		props: {
			visible: true,
			mode: 'chat',
			status: { configured: true },
			statusError: null,
			messages: [],
			streaming: 'x',
			sendError: null,
			canSend: false,
			draft: '',
			send: async () => {},
			onStop: () => {}
		}
	}).body;
	assert.match(html, /\bthink\b/);
	assert.match(html, /data-oj-glass-bubble/);
});

test('no key-setup instruction is spelled anywhere in the component', () => {
	// The issue's exact leak: "Set OPENROUTER_API_KEY in the engine's
	// environment and restart it." Agents read GET /api/v1/assistant/status.
	const source = readFileSync(containerSourcePath, 'utf8');
	assert.doesNotMatch(source, /OPENROUTER_API_KEY|No API key/);
	assert.match(source, /assistantPanelMode\(visible, status, statusError\)/);
});

test('ready heading is exactly OJ', () => {
	const html = renderView();
	assert.match(html, /<h2[^>]*>\s*OJ\s*<\/h2>/);
	assert.match(textOf(html), /OJ is ready/);
});

test('chat aside names OJ and says it is an assistant', () => {
	const html = renderView();
	const asideLabel = html.match(
		/<aside[^>]*class="[^"]*assistant-sidebar[^"]*"[^>]*aria-label="([^"]+)"/
	)?.[1];
	assert.ok(asideLabel, 'chat aside aria-label');
	assert.match(asideLabel, /OJ/);
	assert.match(asideLabel, /assistant/i);
	const textareaLabel = html.match(/<textarea[^>]*aria-label="([^"]+)"/)?.[1];
	assert.ok(textareaLabel, 'textarea aria-label');
	assert.match(textareaLabel, /OJ/);
});

test('unavailable aside names OJ and says it is an assistant', () => {
	const html = renderView({
		mode: 'unavailable',
		status: null,
		statusError: 'HTTP 502'
	});
	const asideLabel = html.match(
		/<aside[^>]*class="[^"]*assistant-unavailable[^"]*"[^>]*aria-label="([^"]+)"/
	)?.[1];
	assert.ok(asideLabel, 'unavailable aside aria-label');
	assert.match(asideLabel, /OJ/);
	assert.match(asideLabel, /assistant/i);
	const human = present.humanAssistantStatusError();
	assert.match(human, /^OJ/);
	assert.match(textOf(html), new RegExp(human.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
	assert.match(html, /data-agent-assistant-error="HTTP 502"/);
});

test('failed reply alert reads with OJ', () => {
	const containerSource = readFileSync(containerSourcePath, 'utf8');
	assert.match(containerSource, /'OJ did not return a reply\. Try again\.'/);
	assert.match(containerSource, /'OJ could not answer right now\. Try again\.'/);
	assert.doesNotMatch(containerSource, /The assistant (did not|could not)/);
	const message = 'OJ did not return a reply. Try again.';
	const html = renderView({ sendError: message });
	assert.match(html, /role="alert"/);
	assert.match(textOf(html), new RegExp(message.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
});

test('no visible string starts with Assistant in the view', () => {
	const source = readFileSync(viewSourcePath, 'utf8');
	assert.doesNotMatch(source, /"Assistant/);
	assert.doesNotMatch(source, />\s*Assistant\b/);
	const html = renderView();
	const h2Text = html.match(/<h2[^>]*>([^<]*)<\/h2>/)?.[1]?.trim();
	assert.equal(h2Text, 'OJ');
});

test('API paths and agent attributes are unchanged', () => {
	assert.equal(api.ASSISTANT_STATUS_PATH, '/api/v1/assistant/status');
	assert.equal(api.ASSISTANT_CHAT_PATH, '/api/v1/assistant/chat');
	const viewSource = readFileSync(viewSourcePath, 'utf8');
	assert.match(viewSource, /data-agent-assistant-error=/);
	assert.match(viewSource, /data-agent-assistant-configured="false"/);
});
