/**
 * Mirror of apps/engine_core/assistant/agent_contract.py, one-to-one.
 *
 * The sidebar and the engine are built in parallel against this pair of
 * files ((private path)). A name that exists on one side and not
 * the other is contract drift; change both or neither.
 *
 * Wire: POST AGENT_PATH with AgentIn, answered `application/x-ndjson`, one
 * AgentEvent per line. Stateless: the client appends every `message` event
 * to its history verbatim and resends the whole history next turn.
 */

export const AGENT_PATH = '/api/v1/assistant/agent';
export const AGENT_MEDIA_TYPE = 'application/x-ndjson';
export const MAX_TOOL_ROUNDS = 8;
export const DECLINED_CODE = 'declined_by_user';

export type Gate = 'read' | 'confirm';
export type Tier = 'instant' | 'fast' | 'think';
export type DoneReason = 'stop' | 'needs_confirmation' | 'turn_cap';

export interface ToolCall {
	id: string;
	name: string;
	arguments: Record<string, unknown>;
}

/** `system` is absent: the engine owns the system prompt. */
export interface AgentMessage {
	role: 'user' | 'assistant' | 'tool';
	content: string | null;
	tool_calls?: ToolCall[] | null;
	tool_call_id?: string | null;
}

export type ContextKind = 'deck' | 'playlist' | 'usb' | 'track';

/** Something the user pointed the assistant at (a composer chip). */
export interface ContextRef {
	kind: ContextKind;
	/** 1-200 characters (MAX_CONTEXT_CHARS). */
	id: string;
	/** 1-200 characters (MAX_CONTEXT_CHARS); the engine flattens it to one line. */
	label: string;
}

export interface AgentIn {
	messages: AgentMessage[];
	/** Held calls the user approved; every other held call is declined. */
	approved_tool_call_ids: string[];
	/** The picker's tier; null is auto. `instant` cannot be forced: the engine treats it as auto. */
	tier?: Tier | null;
	/** Context chips, at most 10 (MAX_CONTEXT_REFS); the engine names them in the system prompt. */
	context?: ContextRef[];
}

export interface ToolError {
	code: string;
	message: string;
}

export type AgentEvent =
	| { type: 'tier'; tier: Tier; model: string | null; reason: string }
	| { type: 'text'; delta: string }
	| { type: 'tool_call'; call: ToolCall; gate: Gate }
	| { type: 'confirm'; call: ToolCall; reason: string }
	| { type: 'tool_result'; call_id: string; ok: boolean; result: unknown; error: ToolError | null }
	| { type: 'message'; message: AgentMessage }
	| { type: 'done'; reason: DoneReason }
	| { type: 'error'; code: string; message: string };

export interface ProviderCatalogHit {
	provider: string;
	provider_label: string;
	catalog_constraint: string;
	title: string;
	in_owned_library?: boolean;
}

export interface ProviderCatalogHitLabel extends ProviderCatalogHit {
	assistant_label: string;
}

export const AGENT_EVENT_TYPES = [
	'tier',
	'text',
	'tool_call',
	'confirm',
	'tool_result',
	'message',
	'done',
	'error'
] as const satisfies readonly AgentEvent['type'][];

/**
 * Chat-kit ToolTrace uses `running` | `ok` | `failed` only. Map agent-session
 * `ToolStatus` rows through `agentToolStatusToTrace` in agent-session.ts
 * (running/held -> running, ok -> ok, failed/declined -> failed).
 */
