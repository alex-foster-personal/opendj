/**
 * NDJSON framing for POST /api/v1/assistant/agent (src/lib/assistant/agent-api.ts),
 * fed through a real ReadableStream whose chunks split lines and characters.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let api;

before(async () => {
	api = await loadTypeScriptModule('src/lib/assistant/agent-api.ts');
});

/** A byte stream that delivers `text` cut at the given byte offsets. */
function bytesStream(text, cuts, onCancel = () => {}) {
	const bytes = new TextEncoder().encode(text);
	const bounds = [0, ...cuts, bytes.length];
	const chunks = bounds.slice(1).map((end, i) => bytes.slice(bounds[i], end));
	return new ReadableStream({
		pull(controller) {
			const next = chunks.shift();
			if (next === undefined) controller.close();
			else controller.enqueue(next);
		},
		cancel: onCancel
	});
}

async function collect(stream) {
	const out = [];
	for await (const event of api.parseAgentStream(stream)) out.push(event);
	return out;
}

const EVENTS = [
	{ type: 'tier', tier: 'fast', model: 'google/gemini-3.7-flash', reason: 'library question' },
	{ type: 'text', delta: 'Deck 2 \u266a caf\u00e9 ' },
	{ type: 'tool_call', call: { id: 'c1', name: 'library_read', arguments: { path: '/api/v1/tracks' } }, gate: 'read' },
	{ type: 'tool_result', call_id: 'c1', ok: true, result: { tracks: [] }, error: null },
	{ type: 'done', reason: 'stop' }
];

test('events survive chunks cut mid-line, mid-character, with blank lines and no final newline', async () => {
	const ndjson = `${EVENTS.map((event) => JSON.stringify(event)).join('\n\n')}`;
	const firstNote = new TextEncoder().encode(ndjson).indexOf(0xe2);
	assert.ok(firstNote > 0, 'fixture must contain a multi-byte character');
	const cuts = [7, firstNote + 1, firstNote + 2, firstNote + 40, firstNote + 41, ndjson.length - 3];
	assert.deepEqual(await collect(bytesStream(ndjson, cuts)), EVENTS);
});

test('one byte per chunk parses the same', async () => {
	const ndjson = `${EVENTS.map((event) => JSON.stringify(event)).join('\n')}\n`;
	const length = new TextEncoder().encode(ndjson).length;
	const cuts = Array.from({ length: length - 1 }, (_, i) => i + 1);
	assert.deepEqual(await collect(bytesStream(ndjson, cuts)), EVENTS);
});

test('an unknown event type is contract drift and throws', async () => {
	const stream = bytesStream('{"type":"tier","tier":"fast","model":null,"reason":"x"}\n{"type":"thought"}\n', [10]);
	await assert.rejects(collect(stream), (error) => {
		assert.equal(error.code, 'assistant_contract_drift');
		assert.match(error.message, /thought/);
		return true;
	});
});

test('a line that is not JSON throws instead of being skipped', async () => {
	await assert.rejects(collect(bytesStream('{"type":"done"\n', [])), (error) => {
		assert.equal(error.code, 'assistant_contract_drift');
		return true;
	});
});

const drift = (error) => {
	assert.equal(error.code, 'assistant_contract_drift');
	return true;
};

test('a clean EOF with no done or error is an error, after the events it did carry', async () => {
	const seen = [];
	const partial = bytesStream(`${JSON.stringify(EVENTS[1])}\n`, []);
	await assert.rejects(async () => {
		for await (const event of api.parseAgentStream(partial)) seen.push(event);
	}, drift);
	assert.deepEqual(seen, [EVENTS[1]], 'what arrived still reaches the session');
	await assert.rejects(collect(bytesStream('', [])), drift);
});

test('a stream that ends in an error event is complete', async () => {
	const failed = { type: 'error', code: 'assistant_upstream_error', message: 'x' };
	assert.deepEqual(await collect(bytesStream(`${JSON.stringify(failed)}\n`, [])), [failed]);
});

const CALL = { id: 'c1', name: 'library_read', arguments: {} };
const VALID = [
	{ type: 'tier', tier: 'instant', model: null, reason: 'r' },
	{ type: 'text', delta: '' },
	{ type: 'tool_call', call: CALL, gate: 'confirm' },
	{ type: 'confirm', call: CALL, reason: 'r' },
	{ type: 'tool_result', call_id: 'c1', ok: false, result: null, error: { code: 'x', message: 'y' } },
	{ type: 'message', message: { role: 'tool', content: '{}', tool_call_id: 'c1' } },
	{ type: 'message', message: { role: 'assistant', content: null, tool_calls: [CALL], tool_call_id: null } },
	{ type: 'message', message: { role: 'assistant', content: 'hi', tool_calls: null } },
	{ type: 'done', reason: 'turn_cap' },
	{ type: 'error', code: 'c', message: 'm' }
];

test('every event in the wire contract parses (control for the rejections below)', () => {
	for (const event of VALID) assert.deepEqual(api.parseAgentLine(JSON.stringify(event)), event);
});

const MALFORMED = [
	{ type: 'done' },
	{ type: 'done', reason: 'finished' },
	{ type: 'tier', tier: 'instant', reason: 'r' },
	{ type: 'tier', tier: 'slow', model: null, reason: 'r' },
	{ type: 'tier', tier: 'fast', model: 7, reason: 'r' },
	{ type: 'text' },
	{ type: 'text', delta: 1 },
	{ type: 'tool_call', gate: 'read' },
	{ type: 'tool_call', call: CALL, gate: 'write' },
	{ type: 'tool_call', call: { id: '', name: 'x', arguments: {} }, gate: 'read' },
	{ type: 'tool_call', call: { id: 'c', name: 'x' }, gate: 'read' },
	{ type: 'tool_call', call: { id: 'c', name: 'x', arguments: [] }, gate: 'read' },
	{ type: 'confirm', call: CALL },
	{ type: 'tool_result', ok: true, result: 1, error: null },
	{ type: 'tool_result', call_id: 'c', ok: 'yes', result: 1, error: null },
	{ type: 'tool_result', call_id: 'c', ok: true, error: null },
	{ type: 'tool_result', call_id: 'c', ok: true, result: 1 },
	{ type: 'tool_result', call_id: 'c', ok: false, result: null, error: 'boom' },
	{ type: 'message' },
	{ type: 'message', message: { role: 'system', content: 'x' } },
	{ type: 'message', message: { role: 'user' } },
	{ type: 'message', message: { role: 'assistant', content: null, tool_calls: [{ id: 'c' }] } },
	{ type: 'message', message: { role: 'tool', content: 'x', tool_call_id: 3 } },
	{ type: 'error', code: 'c' },
	{ type: 'error', message: 'm' }
];

test('a known type with fields that break the contract is drift', () => {
	for (const event of MALFORMED) {
		assert.throws(() => api.parseAgentLine(JSON.stringify(event)), drift, JSON.stringify(event));
	}
});

test('a value that is not an event object is drift, not a crash', () => {
	for (const line of ['null', '[]', '7', '"done"']) assert.throws(() => api.parseAgentLine(line), drift, line);
});

test('a consumer that stops early cancels the body', async () => {
	let cancelled = false;
	const ndjson = `${EVENTS.map((event) => JSON.stringify(event)).join('\n')}\n`;
	const length = new TextEncoder().encode(ndjson).length;
	const cuts = Array.from({ length: Math.floor((length - 1) / 10) }, (_, i) => (i + 1) * 10);
	const stream = bytesStream(ndjson, cuts, () => {
		cancelled = true;
	});
	for await (const event of api.parseAgentStream(stream)) {
		assert.equal(event.type, 'tier');
		break;
	}
	assert.equal(cancelled, true);
});
