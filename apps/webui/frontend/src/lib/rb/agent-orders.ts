/** Execute agent-declared orders through the same performance dispatcher as UI. */
import { releaseBody } from '../api/release-body';
import {
	activePerformanceCommandSession,
	dispatchPerformanceCommand,
	performanceContextTimeNowSec,
	commitPerformanceLaunches,
	performancePositionAnchor,
	performanceStepState,
	performanceForgetStep,
	queryPerformanceState,
	runAudioIoTest,
	type PerformanceCommand
} from './performance-ipc.svelte';
import { durationProgress, resolveDuration, type Duration } from './agent-duration';
// The plan runner itself loads on the first plan order (see `_plans`), so its code
// stays out of /performance's first load; only its types are imported here.
import type { AgentPlanMirror, PlanRunner, PlanStateView } from './agent-plan';
import { autoPlayHandoffInFlight, registerAgentPlanStatus } from './auto-play-plan-gate';
import { everyStemPartSilent } from '$lib/sets/deck-audibility';
import { setPlanOverlay } from './plan-overlay-store.svelte';
import { deckPlayingRefusal, playingDeckReplaced, type DeckPlayingRefusal } from './agent-load-refusal';
import { LEASE_HEADER } from './leased-mirror-publisher';

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
	kind: 'single' | 'sequence' | 'parallel' | 'ramp' | 'plan' | 'plan_amend';
	payload: unknown;
}

interface AgentStepResult {
	status: 'succeeded' | 'failed' | 'skipped';
	error?: string;
	/** AGENT-8332: a known refusal names itself, so the CLI and MCP can say why
	 * and what to send instead (`deck_playing`: the deck, its track, the override). */
	code?: DeckPlayingRefusal['code'];
	detail?: DeckPlayingRefusal['detail'];
	/** browser_select_playlist answers at first rows (`complete` false means more
	 * are coming); a plan answers with its id, status and current bar (AGENT-24);
	 * remote_speaker_answer answers with the speaker's SDP (I18). */
	result?: { complete: boolean; rows_loaded: number } | { sdp: string } | Record<string, unknown>;
}

/** AGENT-24: one transition-plan executor per page, created on first use. */
let _planRunner: PlanRunner | null = null;
/** The installed poll's generation: a runner exists only while one is installed, so
 * an order still in flight when the page closes (its code loading, or not yet
 * asked for) is refused rather than creating a runner nothing will dispose. */
let _planEpoch = 0;
let _planOpenEpoch: number | null = null;
/** The last unmounted page's runner disposal. A new mount's poll waits for it, so no
 * command reaches the engine before that runner has cancelled its launch (BLOCKING, Codex on 9f82b290f4). */
let _retiringPlanDisposal: Promise<void> = Promise.resolve();
const PLAN_CLOSED = 'plan runner disposed: the performance page closed';

async function _plans(): Promise<PlanRunner> {
	const epoch = _planOpenEpoch;
	if (epoch === null) throw new Error(PLAN_CLOSED);
	if (_planRunner) return _planRunner;
	const { createPlanRunner } = await import('./agent-plan');
	if (epoch !== _planOpenEpoch) throw new Error(PLAN_CLOSED);
	_planRunner ??= createPlanRunner({
		dispatch: (command) => dispatchPerformanceCommand(command as PerformanceCommand, undefined, 'agent-command'),
		// A plan keeps ticking in a hidden tab, where only this read commits its incoming launch.
		query: () => {
			commitPerformanceLaunches();
			return queryPerformanceState();
		},
		setTimer: (callback, ms) => window.setTimeout(callback, ms),
		clearTimer: (handle) => window.clearTimeout(handle as number),
		now: () => Date.now(),
		handoffInFlight: autoPlayHandoffInFlight,
		contextNowSec: () => performanceContextTimeNowSec(),
		positionAnchor: (deck) => performancePositionAnchor(deck),
		stepState: (stepId) => performanceStepState(stepId),
		forgetStep: (stepId) => performanceForgetStep(stepId),
		stemsSilent: (deck) => everyStemPartSilent(queryPerformanceState().decks[deck]),
		onOverlay: setPlanOverlay
	});
	return _planRunner;
}

/** AGENT-24: the running (or last) plan, for the UI mirror's `agent_plan`. */
export function readAgentPlanMirror(): AgentPlanMirror | null {
	return _planRunner?.mirror() ?? null;
}

// AGENT-29: AutoPlay stands aside while this runner's plan is scheduled or running.
registerAgentPlanStatus(() => readAgentPlanMirror()?.status ?? null);

async function _planStep(run: () => Promise<Record<string, unknown>>): Promise<AgentStepResult> {
	try {
		return { status: 'succeeded', result: await run() };
	} catch (error) {
		return { status: 'failed', error: _message(error) };
	}
}

/** The control values a ramp reads its start from and travels in. */
const RAMP_TYPES = new Set(['eq', 'fader', 'trim', 'filter', 'stem_gain', 'crossfader', 'tempo']);

/**
 * DECKUX-39: the command types whose OFF only a person may ask for. An agent
 * order is a COMMAND, so an off without `by_user: true` is refused (the engine
 * already answered 422 for it; this is the page's half), never reverted.
 * App-internal paths (restores, the IPC bridge) revert with a WARN instead.
 * Mirrors USER_ONLY_OFF in apps/webui/server/routes/commands.py.
 */
export const USER_ONLY_OFF: Readonly<Record<string, string>> = { quantize: 'Quantize', autoplay: 'AutoPlay' };

export function userOnlyOffRefusal(commandType: string): string {
	return `${commandType} off refused: only a user turns ${USER_ONLY_OFF[commandType]} off; send by_user: true when a person asked for this`;
}

function _command(value: unknown): PerformanceCommand {
	if (value === null || typeof value !== 'object' || Array.isArray(value)) {
		throw new TypeError('agent order command must be an object');
	}
	const record = value as Record<string, unknown>;
	if (typeof record.type === 'string' && record.type in USER_ONLY_OFF && record.enabled === false && record.by_user !== true) {
		throw new Error(userOnlyOffRefusal(record.type));
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

const IO_TEST_BUSES = ['master', 'cue', 'input'] as const;

/** IOPIN-19: the Audio I/O panel's Test, for an agent. Plays the same beeps on
 * the same device the panel's button would, and answers once they sounded.
 * It runs under the dispatcher's ownership (`runAudioIoTest`: a live route
 * session, no preset, the headphone scope held), and an AUDIO IN Test opens
 * the picked microphone and fails unless a level was read from it. */
async function _ioDeviceTest(command: Record<string, unknown>): Promise<Record<string, unknown>> {
	const bus = command.bus;
	if (!IO_TEST_BUSES.includes(bus as (typeof IO_TEST_BUSES)[number])) {
		throw new TypeError(`io_device_test bus must be master, cue or input, got ${JSON.stringify(bus)}`);
	}
	const testBus = bus as (typeof IO_TEST_BUSES)[number];
	// Bound to the route session that sent it: a remount while the Test's code
	// loads must not let it sound on, and answer for, the replacement route.
	const session = activePerformanceCommandSession();
	const [{ ioTestDeviceId, ioTestRefusal }, { playIoTestBeeps, startInputLevelWatch }] = await Promise.all([
		import('$lib/player/io-device-test'),
		import('$lib/player/io-device-test-audio')
	]);
	if (activePerformanceCommandSession() !== session) {
		throw new Error('the performance view changed before the Test sounded. Test again.');
	}
	return runAudioIoTest(async (stillCurrent, signal) => {
		const headphones = queryPerformanceState().mixer.headphones;
		const refusal = ioTestRefusal(testBus, headphones);
		if (refusal !== null) throw new Error(refusal);
		let deviceId = ioTestDeviceId(testBus, headphones);
		const inputId = headphones.selected_input_device_id;
		let samples = 0;
		let peak = 0;
		// The meter's own zero at stop is not a reading of the microphone.
		let reading = true;
		// Set when the microphone stops sending audio after it opened.
		let inputEnded = null as string | null;
		// Throws with the panel's reason when the microphone cannot open.
		const stopInput =
			testBus === 'input'
				? await startInputLevelWatch(
						inputId,
						(rms) => {
							if (!reading) return;
							samples += 1;
							peak = Math.max(peak, rms);
						},
						signal,
						(reason) => {
							inputEnded = reason;
						}
					)
				: null;
		let sounded = false;
		let completed = false;
		// The route tearing down mid-Test aborts `signal`, which cuts this
		// Test's own beeps (never a later Test's) rather than leaving them
		// sounding on audio it no longer owns.
		try {
			if (!stillCurrent()) throw new Error('the performance view closed before the Test sounded');
			if (stopInput !== null) {
				// The microphone open can wait on a permission prompt, and a device
				// change may move MAIN meanwhile: beep, and answer for, the MAIN shown now.
				const now = queryPerformanceState().mixer.headphones;
				const late = ioTestRefusal(testBus, now);
				if (late !== null) throw new Error(late);
				deviceId = ioTestDeviceId(testBus, now);
			}
			completed = await playIoTestBeeps(
				testBus,
				deviceId,
				() => {
					sounded = true;
				},
				signal
			);
		} finally {
			reading = false;
			stopInput?.();
		}
		if (!sounded) throw new Error('another Test started before this one sounded');
		// Cut mid-pattern (the panel closing, another Test, a teardown): it
		// never answers as tested on beeps that did not all play.
		if (!completed) throw new Error('the Test was cut before its beeps finished');
		if (stopInput === null) return { bus: testBus, device_id: deviceId };
		if (inputEnded !== null) throw new Error(inputEnded);
		if (samples === 0) throw new Error('AUDIO IN opened but no level was read from it (is the page hidden?)');
		return { bus: testBus, device_id: deviceId, input_device_id: inputId, input_peak_rms: peak };
	});
}

async function _one(command: unknown): Promise<AgentStepResult> {
	try {
		const order = _command(command) as unknown as Record<string, unknown>;
		if (order.type === 'record_master_tap') await _recordMasterTap(order);
		else if (order.type === 'io_device_test') return { status: 'succeeded', result: await _ioDeviceTest(order) };
		else if (order.type === 'record_master_tap_cancel') {
			// The start gave up on the tap: tear it down, never stream (SET-12).
			(await import('$lib/sets/master-mix-capture')).cancelMasterMixCapture(String(order.session_id));
		} else if (typeof order.type === 'string' && order.type.startsWith('remote_speaker_')) {
			// I18: like record_master_tap, a remote speaker order carries no
			// performance state, so it runs beside the dispatcher, not in it.
			const [{ runRemoteSpeakerCommand }, { remoteSpeakerPageEnv }] = await Promise.all([
				import('$lib/player/remote-speaker-orders'),
				import('./remote-speaker-page-env')
			]);
			const result = await runRemoteSpeakerCommand(
				order,
				remoteSpeakerPageEnv(() => queryPerformanceState().mixer.headphones.selected_master_output_device_id)
			);
			return result === null ? { status: 'succeeded' } : { status: 'succeeded', result };
		} else {
			// AGENT-8332b: read what a replace_playing load is about to stop BEFORE it runs.
			const replaced = playingDeckReplaced(order, queryPerformanceState());
			const after = await dispatchPerformanceCommand(_command(command), undefined, 'agent-command');
			if (order.type === 'browser_select_playlist' && after.browser.load !== null) {
				return { status: 'succeeded', result: { ...after.browser.load } };
			}
			if (replaced !== null) return { status: 'succeeded', result: { replaced_playing: replaced } };
		}
		return { status: 'succeeded' };
	} catch (error) {
		const refusal = deckPlayingRefusal(error, command, queryPerformanceState());
		return refusal === null
			? { status: 'failed', error: _message(error) }
			: { status: 'failed', error: _message(error), ...refusal };
	}
}

/** The live value a ramp travels from, in the unit its command carries. */
function _rampStart(state: ReturnType<typeof queryPerformanceState>, command: PerformanceCommand): number {
	if (command.type === 'eq') return state.mixer.channels[command.deck][`eq_${command.band}`];
	if (command.type === 'stem_gain') return state.decks[command.deck].stems.controls[command.stem].gain;
	if (command.type === 'crossfader') return state.mixer.crossfader;
	if (command.type === 'tempo') return state.decks[command.deck].pitch;
	if (command.type === 'fader' || command.type === 'trim' || command.type === 'filter') {
		return state.mixer.channels[command.deck][command.type];
	}
	throw new TypeError(`ramp has no start value for ${command.type}`);
}

/** A tempo step outside the deck's live pitch range is refused before it is sent. */
function _tempoStepRefusal(deck: ClockDeck, to: number): void {
	const range = queryPerformanceState().decks[deck].pitch_range / 100;
	const min = Math.max(0.01, 1 - range);
	const max = 1 + range;
	if (to < min || to > max) throw new RangeError(`tempo ramp to must be inside deck ${deck}'s pitch range, ${min} to ${max}`);
}

async function _ramp(payload: unknown): Promise<AgentStepResult> {
	if (payload === null || typeof payload !== 'object' || Array.isArray(payload)) throw new TypeError('ramp must be an object');
	const ramp = payload as Record<string, unknown>;
	const command = _command(ramp.command);
	if (!RAMP_TYPES.has(command.type)) {
		throw new TypeError('ramp command must be eq, fader, trim, filter, stem_gain, crossfader, or tempo');
	}
	if (typeof ramp.to !== 'number' || !Number.isFinite(ramp.to)) {
		throw new RangeError('ramp to must be a finite number');
	}
	// AGENT-24: tempo travels in its own unit, a playback ratio; everything else is 0..1.
	if (command.type === 'tempo' ? ramp.to <= 0 || ramp.to > 4 : ramp.to < 0 || ramp.to > 1) {
		throw new RangeError(
			command.type === 'tempo' ? 'tempo ramp to must be a ratio in (0, 4]' : 'ramp to must be a finite 0..1 control value'
		);
	}
	const before = queryPerformanceState();
	if (command.type === 'tempo') {
		// The engine refuses a ratio outside the deck's pitch range, and a ramp dispatches step by step: an out-of-range
		// target would move the deck partway before its first refused step, so the target is checked before any step.
		const range = before.decks[command.deck].pitch_range / 100;
		const min = Math.max(0.01, 1 - range);
		const max = 1 + range;
		if (ramp.to < min || ramp.to > max) {
			throw new RangeError(`tempo ramp to must be inside deck ${command.deck}'s pitch range, ${min} to ${max}`);
		}
	}
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
	const start = _rampStart(before, command);
	const startedAt = performance.now();
	while (true) {
		const progress = over.unit === 'ms'
			? Math.min(1, (performance.now() - startedAt) / over.n)
			: durationProgress(plan, queryPerformanceState().decks[clockDeck].position_ms);
		const value = start + (ramp.to - start) * progress;
		// The operator may narrow the pitch range mid-ramp, so each tempo step is checked against the live range, not the one read at order time.
		if (command.type === 'tempo') _tempoStepRefusal(command.deck, value);
		const step = (command.type === 'tempo' ? { ...command, ratio: value } : { ...command, value }) as PerformanceCommand;
		await dispatchPerformanceCommand(step, undefined, 'agent-command');
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
	} else if (order.kind === 'plan') {
		steps = [await _planStep(async () => (await _plans()).schedule(order.payload))];
	} else if (order.kind === 'plan_amend') {
		steps = [await _planStep(async () => (await _plans()).amend(order.payload))];
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
	isRunning: () => boolean,
	leaseId?: string,
	signal?: AbortSignal
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
			const headers: Record<string, string> = {};
			if (leaseId !== undefined) {
				headers[LEASE_HEADER] = leaseId;
			}
			const init: RequestInit = { headers };
			if (signal !== undefined) {
				init.signal = signal;
			}
			response = await fetch(NEXT_ORDER_URL, init);
		} catch {
			if (!isRunning()) return;
			// Unreachable engine (Safari TypeError `Load failed`) must not
			// kill the loop or become an unhandledrejection.
			await _sleep(IDLE_POLL_MS);
			continue;
		}
		if (response.status === 409) {
			releaseBody(response);
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
			releaseBody(response);
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
			// Reading the claim body here is what releases it on EVERY path below,
			// including a held long poll's `null` (PERF-LEAK-01): never branch on
			// the wait header before this read.
			next = (await response.json()) as ({ id: string } & AgentOrder) | null;
		} catch (error) {
			console.error(`agent order poll: unreadable claim body (${_message(error)}); polling on`);
			await _sleep(CLAIM_RETRY_MS);
			continue;
		}
		if (next !== null) {
			let result: Awaited<ReturnType<typeof executeAgentOrder>>;
			try {
				// The claim can land after this mount retired: a newer mount owns the plan epoch,
				// so a retiring loop refuses the order rather than run it under that epoch.
				result = isRunning()
					? await executeAgentOrder(next)
					: { steps: [{ status: 'failed', error: PLAN_CLOSED }], mirror_delta: { changed: {} } };
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
			releaseBody(complete);
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
	republish: () => void,
	leaseId?: string
): () => Promise<void> {
	let running = true;
	const controller = new AbortController();
	// A mount starts with no runner: one left by an earlier mount is that page's.
	const priorDisposal = _planRunner?.dispose() ?? Promise.resolve();
	_planRunner = null;
	// Inherits any still-pending disposal, so a mount that unmounts before it settles hands the wait on.
	const settled = Promise.all([priorDisposal, _retiringPlanDisposal]).then(() => undefined, () => undefined);
	_retiringPlanDisposal = settled;
	_planEpoch += 1;
	const epoch = _planEpoch;
	_planOpenEpoch = epoch;
	void settled.then(() =>
		pollAgentOrders(page, republish, () => running, leaseId, controller.signal)
	);
	return () => {
		running = false;
		controller.abort();
		if (_planOpenEpoch !== epoch) return Promise.resolve(); // a newer mount owns the runner now
		// The plan runner is page-scoped: a scheduled or running plan must not keep
		// dispatching into an audio engine the route is disposing, and its status
		// must not survive into the next mount. The returned promise settles once the
		// runner has cancelled any launch it fired, so the page keeps its command channel until then.
		_planOpenEpoch = null;
		const disposal = Promise.all([_planRunner?.dispose() ?? Promise.resolve(), settled]).then(() => undefined);
		_planRunner = null;
		_retiringPlanDisposal = disposal;
		return disposal;
	};
}
