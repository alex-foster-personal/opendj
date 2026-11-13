/**
 * Chat kit ToolTrace mid-stream tool steps (#11170).
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const read = (rel) => readFileSync(fileURLToPath(new URL(`../../${rel}`, import.meta.url)), 'utf8');

let session;
let contract;
let types;
let ssr;

before(async () => {
	session = await loadTypeScriptModule('src/lib/assistant/agent-session.ts');
	contract = await loadTypeScriptModule('src/lib/assistant/agent-contract.ts');
	types = await loadTypeScriptModule('src/lib/chat-kit/types.ts');
	ssr = await loadSvelteSsrModule(
		"export { default as ToolTrace } from '$lib/components/chat-kit/ToolTrace.svelte'; export { render } from 'svelte/server';"
	);
});

const call = (id, name, args = {}) => ({ id, name, arguments: args });
const turn = (text, events) =>
	session.foldEvents(session.startTurn(session.EMPTY_SESSION, text), events);

const renderTrace = (steps, streaming = false) =>
	ssr.render(ssr.ToolTrace, { props: { steps, streaming } }).body;

test('[if] a fixture stream has a tool_call event and no done event yet [then] ToolTrace lists a running step', () => {
	const lookup = call('l1', 'library_read', { query: 'techno' });
	const mid = turn('find techno', [{ type: 'tool_call', call: lookup, gate: 'read' }]);
	assert.equal(mid.busy, true);
	assert.equal(mid.done, null);
	const steps = session.toolStepsFromSession(mid);
	assert.deepEqual(steps, [{ id: 'l1', name: 'library_read', status: 'running' }]);
	const body = renderTrace(steps, true);
	assert.match(body, /data-streaming="true"/);
	assert.match(body, /data-step-id="l1"/);
	assert.match(body, /data-status="running"/);
});

test('[if] a later tool_result with ok true arrives [then] the step status is ok', () => {
	const lookup = call('ok-1', 'library_read');
	const after = turn('read', [
		{ type: 'tool_call', call: lookup, gate: 'read' },
		{ type: 'tool_result', call_id: 'ok-1', ok: true, result: { tracks: [] }, error: null }
	]);
	const steps = session.toolStepsFromSession(after);
	assert.deepEqual(steps[0].status, 'ok');
	const body = renderTrace(steps);
	assert.match(body, /data-status="ok"/);
});

test('[if] a later tool_result with ok false arrives [then] the step status is failed', () => {
	const lookup = call('bad-1', 'app_state');
	const after = turn('state', [
		{ type: 'tool_call', call: lookup, gate: 'read' },
		{
			type: 'tool_result',
			call_id: 'bad-1',
			ok: false,
			result: null,
			error: { code: 'no_performance_page', message: 'No Performance page is open.' }
		}
	]);
	const steps = session.toolStepsFromSession(after);
	assert.deepEqual(steps[0].status, 'failed');
	const body = renderTrace(steps);
	assert.match(body, /data-status="failed"/);
});

test('[if] Storybook story ToolTrace MidStream play() runs [then] it asserts a running mid-stream row', () => {
	const story = read('src/lib/components/chat-kit/ToolTrace.stories.ts');
	assert.match(story, /export const MidStream: Story = \{[\s\S]*play: async/);
	const body = renderTrace([{ id: 'mid-1', name: 'library_read', status: 'running' }], true);
	assert.match(body, /data-status="running"/);
	assert.match(body, /library_read/);
});

test('[if] agent-session maps held and declined rows [then] ToolTrace sees running and failed', () => {
	assert.equal(session.agentToolStatusToTrace('held'), 'running');
	assert.equal(session.agentToolStatusToTrace('declined'), 'failed');
	const heldCall = call('h1', 'command');
	const held = turn('go', [
		{ type: 'confirm', call: heldCall, reason: 'audible' },
		{ type: 'done', reason: 'needs_confirmation' }
	]);
	assert.deepEqual(session.toolStepsFromSession(held)[0].status, 'running');
	const declined = turn('nope', [
		{ type: 'tool_call', call: call('d1', 'command'), gate: 'confirm' },
		{
			type: 'tool_result',
			call_id: 'd1',
			ok: false,
			result: null,
			error: { code: contract.DECLINED_CODE, message: 'The user declined this action.' }
		}
	]);
	assert.deepEqual(session.toolStepsFromSession(declined)[0].status, 'failed');
});

test('[if] ToolTrace step statuses are enumerated [then] running, ok, and failed are the only values', () => {
	assert.deepEqual(types.TOOL_TRACE_STEP_STATUSES, ['running', 'ok', 'failed']);
	const contractSrc = read('src/lib/assistant/agent-contract.ts');
	assert.match(contractSrc, /agentToolStatusToTrace/);
});
