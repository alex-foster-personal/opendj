/** Execute agent-declared orders through the same performance dispatcher as UI. */
import {
	dispatchPerformanceCommand,
	queryPerformanceState,
	type PerformanceCommand
} from './performance-ipc.svelte';
import { durationProgress, resolveDuration, type Duration } from './agent-duration';

type ClockDeck = 1 | 2 | 3 | 4;

const NEXT_ORDER_PATH = '/api/v1/commands/next';
const IDLE_POLL_MS = 50;

export interface AgentOrder {
	kind: 'single' | 'sequence' | 'parallel' | 'ramp';
	payload: unknown;
}

interface AgentStepResult {
	status: 'succeeded' | 'failed' | 'skipped';
	error?: string;
}

function _command(value: unknown): PerformanceCommand {
	if (value === null || typeof value !== 'object' || Array.isArray(value)) {
		throw new TypeError('agent order command must be an object');
	}
	return value as PerformanceCommand;
}

function _message(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}

function _changed(before: unknown, after: unknown): unknown {
	if (Object.is(before, after)) return undefined;
	if (before === null || after === null || typeof before !== 'object' || typeof after !== 'object') return after;
	if (Array.isArray(before) || Array.isArray(after)) return after;
	const changed: Record<string, unknown> = {};
	for (const key of new Set([...Object.keys(before), ...Object.keys(after)])) {
		const value = _changed((before as Record<string, unknown>)[key], (after as Record<string, unknown>)[key]);
		if (value !== undefined) changed[key] = value;
	}
	return Object.keys(changed).length === 0 ? undefined : changed;
}

async function _one(command: unknown): Promise<AgentStepResult> {
	try {
		await dispatchPerformanceCommand(_command(command));
		return { status: 'succeeded' };
	} catch (error) {
		return { status: 'failed', error: _message(error) };
	}
}

async function _ramp(payload: unknown): Promise<AgentStepResult> {
	if (payload === null || typeof payload !== 'object' || Array.isArray(payload)) throw new TypeError('ramp must be an object');
	const ramp = payload as Record<string, unknown>;
	const command = _command(ramp.command);
	if (
		command.type !== 'eq' &&
		command.type !== 'fader' &&
		command.type !== 'trim' &&
		command.type !== 'filter' &&
		command.type !== 'stem_gain'
	) {
		throw new TypeError('ramp command must be eq, fader, trim, filter, or stem_gain');
	}
	if (typeof ramp.to !== 'number' || !Number.isFinite(ramp.to) || ramp.to < 0 || ramp.to > 1) {
		throw new RangeError('ramp to must be a finite 0..1 control value');
	}
	const before = queryPerformanceState();
	const over = ramp.over as Duration | undefined;
	if (over === undefined) throw new TypeError('ramp over must declare a Duration');
	const clockDeck: ClockDeck | null = over.clock === undefined || over.clock === 'master'
		? before.master_deck
		: over.clock as ClockDeck;
	if (clockDeck === null) throw new Error('no_master');
	const clock = before.decks[clockDeck];
	const plan = resolveDuration(over, {
		position_ms: clock.position_ms,
		beatgrid: clock.beatgrid,
		phrases: clock.phrases
	});
	if (over.unit !== 'ms' && !clock.playing) throw new Error('clock_not_playing');
	const start =
		command.type === 'eq'
			? before.mixer.channels[command.deck][`eq_${command.band}`]
			: command.type === 'stem_gain'
				? before.decks[command.deck].stems.controls[command.stem].gain
				: before.mixer.channels[command.deck][command.type];
	const startedAt = performance.now();
	while (true) {
		const progress = over.unit === 'ms'
			? Math.min(1, (performance.now() - startedAt) / over.n)
			: durationProgress(plan, queryPerformanceState().decks[clockDeck].position_ms);
		const value = start + (ramp.to - start) * progress;
		await dispatchPerformanceCommand({ ...command, value });
		if (progress === 1) break;
		await new Promise<void>((resolve) => window.setTimeout(resolve, 16));
	}
	return { status: 'succeeded' };
}

export async function executeAgentOrder(order: AgentOrder): Promise<{ steps: AgentStepResult[]; mirror_delta: { changed: unknown } }> {
	const before = queryPerformanceState();
	let steps: AgentStepResult[];
	if (order.kind === 'single') {
		steps = [await _one(order.payload)];
	} else if (order.kind === 'sequence') {
		if (!Array.isArray(order.payload)) throw new TypeError('sequence payload must be an array');
		steps = [];
		for (const command of order.payload) {
			const step = await _one(command);
			steps.push(step);
			if (step.status === 'failed') {
				steps.push(...order.payload.slice(steps.length).map(() => ({ status: 'skipped' as const })));
				break;
			}
		}
	} else if (order.kind === 'parallel') {
		if (!Array.isArray(order.payload)) throw new TypeError('parallel payload must be an array');
		steps = await Promise.all(order.payload.map(_one));
	} else if (order.kind === 'ramp') {
		steps = [await _ramp(order.payload)];
	} else {
		const exhaustive: never = order.kind;
		throw new Error(`unknown agent order ${exhaustive}`);
	}
	return { steps, mirror_delta: { changed: _changed(before, queryPerformanceState()) ?? {} } };
}


// ------------------------------------------------------------------ poll ---

/**
 * The order broker is gated on the engine having a performance page ON RECORD,
 * which happens only once a UI-mirror PUT has been accepted. Until then, and
 * again the moment the mirror is dropped, every `/api/v1/commands` route
 * answers 409 by contract (`apps/webui/server/routes/commands.py`).
 *
 * That precondition therefore has to be readable from here, because a 409 is
 * not something the client can absorb quietly: the BROWSER logs every non-2xx
 * fetch as a console error before app code ever sees the Response, so a poll
 * fired against a page the engine does not have on record lands in the client
 * error log whatever the catch block does. The only way not to log it is not
 * to ask.
 */
export interface PerformancePageRegistration {
	/** True only while the engine has accepted a mirror publish from this page. */
	isRegistered: () => boolean;
	/** The engine answered 409: it no longer has this page on record. */
	forget: () => void;
}

function _sleep(ms: number): Promise<void> {
	return new Promise<void>((resolve) => setTimeout(resolve, ms));
}

/**
 * Claim and execute one agent order at a time for as long as `isRunning()`.
 *
 * Exported for its own unit test: the two behaviors that matter here are an
 * absence (no request at all while unregistered) and a recovery (a 409 stands
 * the loop down instead of killing it), and neither is observable through the
 * installer's fire-and-forget promise.
 */
export async function pollAgentOrders(
	page: PerformancePageRegistration,
	republish: () => void,
	isRunning: () => boolean
): Promise<void> {
	while (isRunning()) {
		if (!page.isRegistered()) {
			await _sleep(IDLE_POLL_MS);
			continue;
		}
		let response: Response;
		try {
			response = await fetch(NEXT_ORDER_PATH);
		} catch {
			// Unreachable engine (Safari TypeError `Load failed`) must not
			// kill the loop or become an unhandledrejection.
			await _sleep(IDLE_POLL_MS);
			continue;
		}
		if (response.status === 409) {
			// Expected state, not a failure: the engine dropped this page between
			// our last accepted publish and this poll (engine restart, or the
			// mirror was closed under us). Stand down until a publish re-registers
			// us rather than hammering a route that can only keep saying 409.
			page.forget();
			console.info('agent orders paused: the engine has no performance page on record');
			await _sleep(IDLE_POLL_MS);
			continue;
		}
		// Every other non-2xx is a real defect and is raised, not absorbed: it
		// reaches the client error log exactly as loudly as any other broken
		// call, because nothing here knows how to make it right.
		if (!response.ok) throw new Error(`agent order poll failed: ${response.status}`);
		const next = (await response.json()) as ({ id: string } & AgentOrder) | null;
		if (next !== null) {
			let result: Awaited<ReturnType<typeof executeAgentOrder>>;
			try {
				result = await executeAgentOrder(next);
			} catch (error) {
				result = { steps: [{ status: 'failed', error: _message(error) }], mirror_delta: { changed: {} } };
			}
			let complete: Response;
			try {
				complete = await fetch(`/api/v1/commands/${next.id}/result`, {
					method: 'POST',
					headers: { 'content-type': 'application/json' },
					body: JSON.stringify(result)
				});
			} catch {
				await _sleep(IDLE_POLL_MS);
				continue;
			}
			if (!complete.ok) throw new Error(`agent order result failed: ${complete.status}`);
			republish();
		}
		await _sleep(IDLE_POLL_MS);
	}
}

/** Run the order poll for the life of the performance route. */
export function installAgentOrderPoll(
	page: PerformancePageRegistration,
	republish: () => void
): () => void {
	let running = true;
	void pollAgentOrders(page, republish, () => running);
	return () => {
		running = false;
	};
}
