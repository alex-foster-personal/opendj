/**
 * The agent sidebar's transcript fold (src/lib/assistant/agent-session.ts),
 * driven with AgentEvents in the exact wire shape of agent-contract.ts.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let s;
let contract;

before(async () => {
	s = await loadTypeScriptModule('src/lib/assistant/agent-session.ts');
	contract = await loadTypeScriptModule('src/lib/assistant/agent-contract.ts');
});

const call = (id, name, args = {}) => ({ id, name, arguments: args });
const turn = (text, events) => s.foldEvents(s.startTurn(s.EMPTY_SESSION, text), events);
const tools = (session) => session.items.filter((item) => item.kind === 'tool');

test('a confirm with no prior tool_call still yields one held, confirm-gated card', () => {
	const volume = call('c1', 'system_volume_set', { level: 30 });
	const session = turn('volume to 30', [
		{ type: 'tier', tier: 'instant', model: null, reason: 'matched the system volume intent' },
		{ type: 'confirm', call: volume, reason: 'Changes the volume of this computer.' },
		{ type: 'done', reason: 'needs_confirmation' }
	]);
	const [card] = tools(session);
	assert.equal(tools(session).length, 1);
	assert.equal(card.status, 'held');
	assert.equal(card.gate, 'confirm');
	assert.equal(card.reason, 'Changes the volume of this computer.');
	assert.deepEqual(s.heldCalls(session), [volume]);
	assert.equal(session.busy, false);
	assert.equal(session.done, 'needs_confirmation');
});

test('a confirm after its tool_call updates that card instead of adding one', () => {
	const play = call('c2', 'command', { command: 'play deck 2' });
	const session = turn('play deck 2', [
		{ type: 'tool_call', call: play, gate: 'confirm' },
		{ type: 'confirm', call: play, reason: 'Deck 1 is audible.' }
	]);
	assert.equal(tools(session).length, 1);
	assert.equal(tools(session)[0].status, 'held');
	assert.equal(tools(session)[0].reason, 'Deck 1 is audible.');
});

test('a declined_by_user result is declined, any other failure is failed, ok is ok', () => {
	const [a, b, c] = [call('a', 'command'), call('b', 'app_state'), call('c', 'library_read')];
	const held = turn('go', [
		{ type: 'confirm', call: a, reason: 'audible' },
		{ type: 'tool_call', call: b, gate: 'read' },
		{ type: 'tool_call', call: c, gate: 'read' },
		{ type: 'done', reason: 'needs_confirmation' }
	]);
	const session = s.foldEvents(s.resumeTurn(held), [
		{
			type: 'tool_result',
			call_id: 'a',
			ok: false,
			result: null,
			error: { code: contract.DECLINED_CODE, message: 'The user declined this action.' }
		},
		{
			type: 'tool_result',
			call_id: 'b',
			ok: false,
			result: null,
			error: { code: 'no_performance_page', message: 'No Performance page is open.' }
		},
		{ type: 'tool_result', call_id: 'c', ok: true, result: { tracks: [] }, error: null },
		{ type: 'done', reason: 'stop' }
	]);
	assert.deepEqual(
		tools(session).map((item) => [item.call.id, item.status]),
		[
			['a', 'declined'],
			['b', 'failed'],
			['c', 'ok']
		]
	);
	assert.equal(tools(session)[1].error.code, 'no_performance_page');
	assert.deepEqual(tools(session)[2].result, { tracks: [] });
	assert.deepEqual(s.heldCalls(session), []);
});

test('text deltas join into one item until a tool row breaks the run', () => {
	const lookup = call('l1', 'library_read', { query: 'techno 128' });
	const session = turn('find techno', [
		{ type: 'text', delta: 'Look' },
		{ type: 'text', delta: 'ing...' },
		{ type: 'tool_call', call: lookup, gate: 'read' },
		{ type: 'text', delta: 'Found ' },
		{ type: 'text', delta: 'three.' }
	]);
	assert.deepEqual(
		session.items.map((item) => (item.kind === 'text' ? item.text : item.kind)),
		['user', 'Looking...', 'tool', 'Found three.']
	);
	assert.equal(session.busy, true, 'no done yet: still streaming');
});

test('message events feed the resent history verbatim and never the visible items', () => {
	const lookup = call('l1', 'library_read', { query: 'techno 128' });
	const asked = { role: 'assistant', content: null, tool_calls: [lookup] };
	const answered = { role: 'tool', content: '{"tracks":[]}', tool_call_id: 'l1' };
	const session = turn('find techno', [
		{ type: 'message', message: asked },
		{ type: 'message', message: answered },
		{ type: 'done', reason: 'stop' }
	]);
	assert.deepEqual(
		session.items.map((item) => item.kind),
		['user']
	);
	assert.deepEqual(s.requestFor(session, ['x']), {
		messages: [{ role: 'user', content: 'find techno' }, asked, answered],
		approved_tool_call_ids: ['x'],
		tier: null,
		context: []
	});
});

test('the picker tier and context chips ride on the request', () => {
	const session = s.startTurn(s.EMPTY_SESSION, 'what is on deck 2?');
	const context = [{ kind: 'deck', id: '2', label: 'Deck 2' }];
	const body = s.requestFor(session, [], { tier: 'think', context });
	assert.equal(body.tier, 'think');
	assert.deepEqual(body.context, context);
});

test('an error event is shown and ends the turn without a done', () => {
	const session = turn('hello', [
		{ type: 'tier', tier: 'fast', model: 'google/gemini-3.7-flash', reason: 'chat' },
		{ type: 'error', code: 'assistant_upstream_error', message: 'The model service answered 502.' }
	]);
	assert.deepEqual(session.items.at(-1), {
		kind: 'error',
		code: 'assistant_upstream_error',
		message: 'The model service answered 502.'
	});
	assert.equal(session.busy, false);
	assert.equal(session.done, null);
});

test('a resumed response keeps the one tier chip of its user turn; the next turn gets its own', () => {
	const tier = { type: 'tier', tier: 'instant', model: null, reason: 'rule: deck transport' };
	const held = turn('pause deck 4', [
		tier,
		{ type: 'confirm', call: call('instant_1', 'command'), reason: 'r' }
	]);
	const resumed = s.foldEvents(s.resumeTurn(held), [
		{ ...tier, reason: 'resume: answer the held instant call' },
		{ type: 'tool_result', call_id: 'instant_1', ok: true, result: {}, error: null },
		{ type: 'done', reason: 'stop' }
	]);
	const next = s.foldEvents(s.startTurn(resumed, 'play deck 1'), [tier]);
	assert.deepEqual(
		next.items.map((item) => (item.kind === 'tier' ? `tier:${item.reason}` : item.kind)),
		['user', 'tier:rule: deck transport', 'tool', 'user', 'tier:rule: deck transport']
	);
});

test('resumeTurn reopens the turn without a new user message; endTurn only clears busy', () => {
	const held = turn('play deck 2', [{ type: 'done', reason: 'needs_confirmation' }]);
	const resumed = s.resumeTurn(held);
	assert.equal(resumed.busy, true);
	assert.equal(resumed.done, null);
	assert.equal(resumed.history.length, 1);
	assert.deepEqual(s.endTurn(resumed), { ...resumed, busy: false });
});
