/**
 * The sidebar's transcript, folded from AgentEvents. Pure: no fetch, no
 * runes, no clock, so the live sidebar and its Storybook states run the
 * exact same fold over the exact same wire shapes (agent-contract.ts).
 *
 * `history` is what the next request resends: user turns plus every
 * `message` event verbatim. `items` is what the reader sees.
 */
import type { ToolStepRef, ToolTraceStepStatus } from '$lib/chat-kit/types';

import {
	DECLINED_CODE,
	type AgentEvent,
	type AgentIn,
	type AgentMessage,
	type ContextRef,
	type DoneReason,
	type Gate,
	type Tier,
	type ToolCall,
	type ToolError
} from './agent-contract';

export type ToolStatus = 'running' | 'held' | 'ok' | 'failed' | 'declined';

export type Item =
	| { kind: 'user'; text: string }
	| { kind: 'tier'; tier: Tier; model: string | null; reason: string }
	| { kind: 'text'; text: string }
	| {
			kind: 'tool';
			call: ToolCall;
			gate: Gate;
			status: ToolStatus;
			reason: string | null;
			result: unknown;
			error: ToolError | null;
	  }
	| { kind: 'error'; code: string; message: string };

export interface Session {
	items: Item[];
	history: AgentMessage[];
	busy: boolean;
	done: DoneReason | null;
}

export const EMPTY_SESSION: Session = { items: [], history: [], busy: false, done: null };

//----- turn lifecycle

export function startTurn(session: Session, text: string): Session {
	return {
		items: [...session.items, { kind: 'user', text }],
		history: [...session.history, { role: 'user', content: text }],
		busy: true,
		done: null
	};
}

/** Resume after the user answered held calls; the engine declines the rest. */
export function resumeTurn(session: Session): Session {
	return { ...session, busy: true, done: null };
}

export function endTurn(session: Session): Session {
	return { ...session, busy: false };
}

export interface RequestOptions {
	/** null is auto. */
	tier?: Tier | null;
	context?: ContextRef[];
}

export function requestFor(
	session: Session,
	approved: string[] = [],
	options: RequestOptions = {}
): AgentIn {
	return {
		messages: session.history,
		approved_tool_call_ids: approved,
		tier: options.tier ?? null,
		context: options.context ?? []
	};
}

export function heldCalls(session: Session): ToolCall[] {
	return session.items.flatMap((item) =>
		item.kind === 'tool' && item.status === 'held' ? [item.call] : []
	);
}

//----- event fold

export function applyEvent(session: Session, event: AgentEvent): Session {
	switch (event.type) {
		case 'tier':
			return turnHasTier(session)
				? session
				: push(session, {
						kind: 'tier',
						tier: event.tier,
						model: event.model,
						reason: event.reason
					});
		case 'text':
			return appendText(session, event.delta);
		case 'tool_call':
			return push(session, toolItem(event.call, event.gate, 'running', null));
		case 'confirm':
			return upsertTool(session, event.call, (item) => ({
				...item,
				status: 'held',
				reason: event.reason
			}));
		case 'tool_result':
			return mapTool(session, event.call_id, (item) => ({
				...item,
				status: event.ok ? 'ok' : event.error?.code === DECLINED_CODE ? 'declined' : 'failed',
				result: event.result,
				error: event.error
			}));
		case 'message':
			return { ...session, history: [...session.history, event.message] };
		case 'done':
			return { ...session, busy: false, done: event.reason };
		case 'error':
			return {
				...push(session, { kind: 'error', code: event.code, message: event.message }),
				busy: false
			};
	}
}

export function foldEvents(session: Session, events: AgentEvent[]): Session {
	return events.reduce(applyEvent, session);
}

/** Map one agent tool row to the kit-local ToolTrace step status. */
export function agentToolStatusToTrace(status: ToolStatus): ToolTraceStepStatus {
	switch (status) {
		case 'running':
		case 'held':
			return 'running';
		case 'ok':
			return 'ok';
		case 'failed':
		case 'declined':
			return 'failed';
		default: {
			const _exhaustive: never = status;
			throw new Error(`unknown tool status: ${String(_exhaustive)}`);
		}
	}
}

/** Tool rows for ToolTrace; safe while `session.busy` before a terminal `done`. */
export function toolStepsFromSession(session: Session): ToolStepRef[] {
	return toolStepsFromItems(session.items);
}

export function toolStepsFromItems(items: Item[]): ToolStepRef[] {
	return items.flatMap((item) =>
		item.kind === 'tool'
			? [{ id: item.call.id, name: item.call.name, status: agentToolStatusToTrace(item.status) }]
			: []
	);
}

//----- helpers

type ToolItem = Extract<Item, { kind: 'tool' }>;

function push(session: Session, item: Item): Session {
	return { ...session, items: [...session.items, item] };
}

function toolItem(call: ToolCall, gate: Gate, status: ToolStatus, reason: string | null): ToolItem {
	return { kind: 'tool', call, gate, status, reason, result: null, error: null };
}

/** One chip per user turn: a resumed response keeps the chip of the turn it answers. */
function turnHasTier(session: Session): boolean {
	const start = session.items.findLastIndex((item) => item.kind === 'user');
	return session.items.slice(start + 1).some((item) => item.kind === 'tier');
}

function appendText(session: Session, delta: string): Session {
	const last = session.items.at(-1);
	if (last?.kind !== 'text') return push(session, { kind: 'text', text: delta });
	return {
		...session,
		items: [...session.items.slice(0, -1), { kind: 'text', text: last.text + delta }]
	};
}

function mapTool(session: Session, callId: string, change: (item: ToolItem) => ToolItem): Session {
	return {
		...session,
		items: session.items.map((item) =>
			item.kind === 'tool' && item.call.id === callId ? change(item) : item
		)
	};
}

/** A confirm may follow its tool_call or arrive alone; either way one card. */
function upsertTool(
	session: Session,
	call: ToolCall,
	change: (item: ToolItem) => ToolItem
): Session {
	const known = session.items.some((item) => item.kind === 'tool' && item.call.id === call.id);
	return known
		? mapTool(session, call.id, change)
		: push(session, change(toolItem(call, 'confirm', 'held', null)));
}
