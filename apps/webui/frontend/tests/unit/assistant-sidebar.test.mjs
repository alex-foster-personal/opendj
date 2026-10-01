/**
 * Assistant sidebar presentation during first-run setup.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function read(relative) {
	return readFileSync(fileURLToPath(new URL(`../../${relative}`, import.meta.url)), 'utf8');
}

test('an unconfigured assistant renders no operator panel', () => {
	const source = read('src/lib/components/assistant/AssistantSidebar.svelte');
	assert.match(source, /showPanel/);
	assert.match(source, /status\.configured === true/);
	assert.doesNotMatch(source, /OPENROUTER_API_KEY/);
	assert.doesNotMatch(source, /No API key/);
	assert.match(source, /data-agent-assistant-configured="false"/);
});

test('configured assistants still render the chat surface', () => {
	const source = read('src/lib/components/assistant/AssistantSidebar.svelte');
	assert.match(source, /Ask about your library/);
	assert.match(source, /getAssistantStatus/);
});

test('assistant status errors show actionable copy with agent diagnostics', () => {
	const source = read('src/lib/components/assistant/AssistantSidebar.svelte');
	assert.match(source, /humanAssistantStatusError\(\)/);
	assert.match(source, /assistant-unavailable/);
	assert.match(source, /data-agent-assistant-error=\{statusError\}/);
	assert.doesNotMatch(source, /\{statusError\}<\/pre>\s*<\/details>\s*\{:else if visible && status !== null/);
});
