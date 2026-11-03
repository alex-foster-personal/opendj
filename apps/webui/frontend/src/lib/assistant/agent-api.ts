/**
 * Client for POST /api/v1/assistant/agent (agent-contract.ts). Yields one
 * AgentEvent per NDJSON line; a line that is not a whole contract event, or
 * a stream that ends with no `done` or `error`, is drift and throws rather
 * than being skipped.
 */
import { API_BASE } from '$lib/api/base';

import { AGENT_EVENT_TYPES, AGENT_PATH, type AgentEvent, type AgentIn } from './agent-contract';
import { AssistantError, assistantRefusal } from './assistant-api';

const KNOWN = new Set<string>(AGENT_EVENT_TYPES);

//----- wire shapes, one rule per field of agent-contract.ts

type Wire = Record<string, unknown>;

const isWire = (value: unknown): value is Wire =>
	typeof value === 'object' && value !== null && !Array.isArray(value);
const isText = (value: unknown): value is string => typeof value === 'string';
const isIdText = (value: unknown): boolean => isText(value) && value !== '';
const isOneOf = (value: unknown, ...allowed: string[]): boolean => isText(value) && allowed.includes(value);
const isTextOrNull = (value: unknown): boolean => value === null || isText(value);
const isAbsentOr = (value: unknown, check: (present: unknown) => boolean): boolean =>
	value === undefined || value === null || check(value);

const isToolCall = (value: unknown): boolean =>
	isWire(value) && isIdText(value.id) && isIdText(value.name) && isWire(value.arguments);
const isToolError = (value: unknown): boolean => isWire(value) && isText(value.code) && isText(value.message);
const isMessage = (value: unknown): boolean =>
	isWire(value) &&
	isOneOf(value.role, 'user', 'assistant', 'tool') &&
	isTextOrNull(value.content) &&
	isAbsentOr(value.tool_calls, (calls) => Array.isArray(calls) && calls.every(isToolCall)) &&
	isAbsentOr(value.tool_call_id, isText);

/** Keyed by the union, so a new event type does not compile until it has a rule. */
const VALID_EVENT: Record<AgentEvent['type'], (event: Wire) => boolean> = {
	tier: (e) => isOneOf(e.tier, 'instant', 'fast', 'think') && isTextOrNull(e.model) && isText(e.reason),
	text: (e) => isText(e.delta),
	tool_call: (e) => isToolCall(e.call) && isOneOf(e.gate, 'read', 'confirm'),
	confirm: (e) => isToolCall(e.call) && isText(e.reason),
	tool_result: (e) =>
		isText(e.call_id) &&
		typeof e.ok === 'boolean' &&
		'result' in e &&
		(e.error === null || isToolError(e.error)),
	message: (e) => isMessage(e.message),
	done: (e) => isOneOf(e.reason, 'stop', 'needs_confirmation', 'turn_cap'),
	error: (e) => isText(e.code) && isText(e.message)
};

export function parseAgentLine(line: string): AgentEvent {
	let value: unknown;
	try {
		value = JSON.parse(line);
	} catch {
		throw new AssistantError('assistant_contract_drift', `not JSON: ${line.slice(0, 120)}`);
	}
	const type = (value as { type?: unknown } | null)?.type;
	if (typeof type !== 'string' || !KNOWN.has(type)) {
		throw new AssistantError('assistant_contract_drift', `unknown event type: ${String(type)}`);
	}
	if (!VALID_EVENT[type as AgentEvent['type']](value as Wire)) {
		throw new AssistantError('assistant_contract_drift', `malformed ${type} event: ${line.slice(0, 120)}`);
	}
	return value as AgentEvent;
}

export async function* parseAgentStream(body: ReadableStream<Uint8Array>): AsyncGenerator<AgentEvent> {
	const reader = body.getReader();
	const decoder = new TextDecoder();
	let buffer = '';
	let terminal = false;
	try {
		for (;;) {
			const { value, done } = await reader.read();
			buffer += decoder.decode(value, { stream: !done });
			const lines = buffer.split('\n');
			buffer = done ? '' : (lines.pop() ?? '');
			for (const line of lines) {
				if (!line.trim()) continue;
				const event = parseAgentLine(line);
				terminal ||= event.type === 'done' || event.type === 'error';
				yield event;
			}
			if (done && !terminal) {
				throw new AssistantError('assistant_contract_drift', 'the agent stream ended with no done or error event');
			}
			if (done) return;
		}
	} finally {
		await reader.cancel().catch(() => undefined);
	}
}

export async function* streamAgent(input: AgentIn, signal: AbortSignal): AsyncGenerator<AgentEvent> {
	const response = await fetch(`${API_BASE}${AGENT_PATH}`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(input),
		signal
	});
	if (!response.ok) throw await assistantRefusal(response);
	if (!response.body) throw new AssistantError('assistant_no_stream', 'the agent reply had no body');
	yield* parseAgentStream(response.body);
}
