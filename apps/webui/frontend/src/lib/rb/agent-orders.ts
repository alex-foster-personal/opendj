/** Execute agent-declared orders through the same performance dispatcher as UI. */
import {
	dispatchPerformanceCommand,
	queryPerformanceState,
	type PerformanceCommand
} from './performance-ipc.svelte';
import { durationProgress, resolveDuration, type Duration } from './agent-duration';

type ClockDeck = 1 | 2 | 3 | 4;

const NEXT_ORDER_PATH = '/api/v1/commands/next';
/**
 * AGENT-19: how long the engine holds each claim request open. The claim is a
 * LONG POLL, not a timer: a hidden tab's timers are throttled (Chrome aligns
 * them to 1 s, and a timer chain to one wake-up per minute after five hidden
 * minutes) but a network response is delivered at once, so the next order
 * reaches a backgrounded leader as soon as it is posted. Must stay at or
 * under the engine's `ORDER_WAIT_MAX_MS` (30 s, `routes/commands.py`).
 */
export const ORDER_LONG_POLL_MS = 20_000;
/** The engine echoes the hold it honoured here; absent means it did not hold. */
export const ORDER_WAIT_HEADER = 'x-opendj-order-wait-ms';
export const NEXT_ORDER_URL = `${NEXT_ORDER_PATH}?wait_ms=${ORDER_LONG_POLL_MS}`;
/**
 * The timer FALLBACK, used only when the long poll cannot run: the engine is
 * unreachable, has not registered this page yet, or answered without holding.
 * Expected latency on that path is 50 ms in a visible tab, about 1 s in a
 * hidden one, and up to 60 s per hop after five hidden minutes.
 */
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

/** SET-12: a master REC start pushes this order so the tap connects at once
 *  instead of at the REC rail's next status poll (which lost ~4.5 s). */
async function _recordMasterTap(command: Record<string, unknown>): Promise<void> {
	if (typeof command.session_id !== 'string') throw new TypeError('record_master_tap needs session_id');
	await (await import('$lib/sets/master-mix-capture')).ensureMasterMixCapture(command.session_id);
}

async function _one(command: unknown): Promise<AgentStepResult> {
	try {
		const order = _command(command) as unknown as Record<string, unknown>;
		if (order.type === 'record_master_tap') await _recordMasterTap(order);
		else await dispatchPerformanceCommand(_command(command));
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
	/** AGENT-19: settles when the page is next registered, so the poll starts at
	 * once instead of after a timer a hidden tab would throttle. */
	whenRegistered: () => Promise<void>;
	/** The engine answered 409: it no longer has this page on record. */
	forget: () => void;
}

/** How long the loop waits after a failed claim before asking again. */
export const CLAIM_RETRY_MS = 1_000;

/**
 * The engine's answer to an order result. Never fatal: the order already ran
 * on this page, and a dead loop would stop every later one.
 * 404/410: the engine withdrew the order (its waiter gave up), expected.
 */
function _reportResultAnswer(orderId: string, status: number): void {
	if (status >= 200 && status < 300) return;
	if (status === 404 || status === 410) {
		console.info(`agent order ${orderId}: the engine had withdrawn it (${status}); polling on`);
		return;
	}
	console.warn(`agent order ${orderId}: result answered ${status}; polling on`);
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
	let reportedUnheld = false;
	let reportedClaimFailure = false;
	while (isRunning()) {
		if (!page.isRegistered()) {
			// Registration wakes the loop at once; the timer only re-checks
			// isRunning() and is the documented fallback.
			await Promise.race([page.whenRegistered(), _sleep(IDLE_POLL_MS)]);
			continue;
		}
		let response: Response;
		try {
			response = await fetch(NEXT_ORDER_URL);
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
		// Every other non-2xx is a real defect: loud (console.error reaches the
		// client error log), once per outage, and NEVER fatal. A dead loop
		// silently disables every agent command on this page (play, autoplay,
		// the watchdog's resume), so only the route's uninstall ends it.
		if (!response.ok) {
			if (!reportedClaimFailure) {
				console.error(`agent order poll failed: ${response.status}; retrying every ${CLAIM_RETRY_MS} ms`);
				reportedClaimFailure = true;
			}
			await _sleep(CLAIM_RETRY_MS);
			continue;
		}
		if (reportedClaimFailure) {
			console.info('agent order poll recovered');
			reportedClaimFailure = false;
		}
		let next: ({ id: string } & AgentOrder) | null;
		try {
			next = (await response.json()) as ({ id: string } & AgentOrder) | null;
		} catch (error) {
			console.error(`agent order poll: unreadable claim body (${_message(error)}); polling on`);
			await _sleep(CLAIM_RETRY_MS);
			continue;
		}
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
			} catch (error) {
				console.warn(`agent order ${next.id}: result could not be posted (${_message(error)}); polling on`);
				await _sleep(IDLE_POLL_MS);
				continue;
			}
			_reportResultAnswer(next.id, complete.status);
			republish();
			// Straight back to the long poll: a timer here would cost a hidden
			// tab up to a minute before it could even ask for the next order.
			continue;
		}
		if (response.headers.get(ORDER_WAIT_HEADER) === String(ORDER_LONG_POLL_MS)) {
			// The engine held the request for the window and nothing came: ask
			// again at once, which is what keeps the next order timer-free.
			reportedUnheld = false;
			continue;
		}
		if (!reportedUnheld) {
			// An engine that predates AGENT-19 ignores wait_ms. Loud, once per
			// transition, because a hidden tab now pays the throttled timer.
			console.error(
				`agent orders: the engine did not hold ${NEXT_ORDER_URL}; falling back to a ` +
					`${IDLE_POLL_MS} ms timer poll, which a hidden tab throttles to 1 s or 60 s`
			);
			reportedUnheld = true;
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
