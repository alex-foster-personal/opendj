/**
 * The approve card's words come from the held call's own arguments
 * (AgentSidebarView.svelte passes `item.call.arguments` to `toolLabel`).
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let ssr;
let labels;

before(async () => {
	labels = await loadTypeScriptModule('src/lib/assistant/agent-labels.ts');
	ssr = await loadSvelteSsrModule(
		[
			"export { default as AgentSidebarView } from '$lib/components/assistant/AgentSidebarView.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

const noop = () => {};

function heldCard(name, args) {
	const item = {
		kind: 'tool',
		call: { id: 'call-1', name, arguments: args },
		gate: 'confirm',
		status: 'held',
		reason: null,
		result: null,
		error: null
	};
	return ssr.render(ssr.AgentSidebarView, {
		props: {
			items: [item],
			busy: false,
			done: 'needs_confirmation',
			availability: 'ready',
			ontier: noop,
			onsend: noop,
			onapprove: noop,
			ondecline: noop,
			onstop: noop,
			onnewchat: noop,
			onclose: noop
		}
	}).body;
}

test('a held typed call reads as the sentence its arguments make', () => {
	/** [if] the card drops the held call's arguments and shows the argument-free sentence [then] fail, [else stop]. */
	const args = { playlist_id: 'pl-3', name: 'Warm up', etag: 'e1' };
	const want = labels.toolLabel('playlist_rename', args);
	assert.equal(want, 'Rename playlist pl-3 to "Warm up"');
	const body = heldCard('playlist_rename', args);
	assert.ok(body.includes('aria-label="Rename playlist pl-3 to &quot;Warm up&quot;"'), body);
	assert.ok(
		!body.includes(`aria-label="${labels.toolLabel('playlist_rename')}"`),
		'not the argument-free sentence'
	);
});

test('a held unknown tool shows its name and its arguments on the card', () => {
	/** [if] an unknown held call shows only its bare name [then] fail, [else stop]. */
	const body = heldCard('shell_run', { cmd: 'ls' });
	assert.ok(body.includes('aria-label="shell_run (cmd: ls)"'), body);
});
