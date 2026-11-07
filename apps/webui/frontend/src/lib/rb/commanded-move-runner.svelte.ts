/**
 * D3 (#9373, issue #9454): drive a dial or fader from a commanded move plan,
 * sampled once per frame on the engine presentation clock (`performance.now()`),
 * the same domain `playhead-clock.ts` anchors are projected in (`presentedSampleAtMs`,
 * `_frameNowMs` in `playhead-display.svelte.ts`). Plans built from playhead or beat
 * times must be expressed in this domain; D1/D2 own that conversion. Written through
 * `setKnobFromBaseline` so Alt links and stagger apply without accumulating across
 * frames. The hand wins: any drag, wheel, arrow nudge, pad or selection move cancels
 * the move. D2 (#9453) and D5 (#9456) build plans and call `startCommandedMove`.
 *
 * MIDI and other non-hand writers are not detected yet; that is out of scope here.
 */

import { beginAnimation, endAnimation } from './dial-animations.svelte';
import { frameOrTimeout } from './frame-backstop';
import {
	linkedPartnerId,
	onKnobHandInput,
	readKnobValue,
	setKnobFromBaseline
} from './knob-control.svelte';

const REPLACE_REASON = 'replaced by a newer animation on this control';

/** Structural twin of D1's plan (#9452). All times are in the engine presentation clock
 * domain (performance.now() ms); a plan built in track-position ms must be
 * converted by its builder first. */
export interface CommandedMovePlan {
	readonly startMs: number;
	readonly endMs: number;
	valueAt(tMs: number): number;
}

export type CommandedMoveOutcome = 'completed' | 'cancelled' | 'replaced';

export interface CommandedMoveResult {
	outcome: CommandedMoveOutcome;
	reason: string | null;
}

export interface CommandedMoveRecord {
	control_id: string;
	planned_end_ms: number;
	actual_end_ms: number;
}

export interface CommandedMoveHandle {
	readonly controlId: string;
	readonly plan: CommandedMovePlan;
	readonly done: Promise<CommandedMoveResult>;
	cancel(reason?: string): void;
}

export interface CommandedMoveClock {
	now: () => number;
	nextFrame: () => Promise<void>;
}

const MOVE_LOG_CAP = 256;

const _defaultClock: CommandedMoveClock = {
	now: () => performance.now(),
	nextFrame: () => frameOrTimeout()
};

let _clock: CommandedMoveClock = _defaultClock;

const _active = new Map<string, _Move>();
const _log: CommandedMoveRecord[] = [];

let _handSubscribed = false;
let _handUnsub: (() => void) | null = null;

class _Move {
	readonly controlId: string;
	readonly plan: CommandedMovePlan;
	token: symbol;
	stopped = false;
	startPrimary: number;
	startPartner: number | null;
	partnerId: string | null;
	lastWritten: number | null = null;
	lastWrittenPartner: number | null = null;
	private readonly _resolve: (r: CommandedMoveResult) => void;
	private readonly _reject: (e: unknown) => void;
	readonly done: Promise<CommandedMoveResult>;

	constructor(
		controlId: string,
		plan: CommandedMovePlan,
		token: symbol,
		startPrimary: number,
		partnerId: string | null,
		startPartner: number | null
	) {
		this.controlId = controlId;
		this.plan = plan;
		this.token = token;
		this.startPrimary = startPrimary;
		this.partnerId = partnerId;
		this.startPartner = startPartner;
		let resolve!: (r: CommandedMoveResult) => void;
		let reject!: (e: unknown) => void;
		this.done = new Promise<CommandedMoveResult>((res, rej) => {
			resolve = res;
			reject = rej;
		});
		this._resolve = resolve;
		this._reject = reject;
	}

	stop(outcome: CommandedMoveOutcome, reason: string | null): void {
		if (this.stopped) return;
		this.stopped = true;
		this._resolve({ outcome, reason });
	}

	fail(err: unknown): void {
		if (this.stopped) return;
		this.stopped = true;
		this._reject(err);
	}

	cancel(reason: string = 'cancelled'): void {
		this.stop('cancelled', reason);
	}
}

function _outcomeForCancelReason(reason: string): CommandedMoveOutcome {
	return reason === REPLACE_REASON ? 'replaced' : 'cancelled';
}

function _pushLog(record: CommandedMoveRecord): void {
	_log.push(record);
	if (_log.length > MOVE_LOG_CAP) _log.shift();
}

function _ensureHandSubscription(): void {
	if (_handSubscribed) return;
	_handUnsub = onKnobHandInput((ids) => {
		for (const id of ids) _active.get(id)?.stop('cancelled', 'moved by hand');
	});
	_handSubscribed = true;
}

function _rebaselineIfLinkChanged(move: _Move): void {
	const current = linkedPartnerId(move.controlId);
	if (current === move.partnerId) return;
	move.startPrimary = move.lastWritten ?? readKnobValue(move.controlId) ?? move.startPrimary;
	move.partnerId = current;
	move.startPartner = current === null ? null : readKnobValue(current);
}

export function startCommandedMove(
	controlId: string,
	buildPlan: (fromValue: number, nowMs: number) => CommandedMovePlan,
	label: string = 'commanded move'
): CommandedMoveHandle | null {
	_ensureHandSubscription();
	const prior = _active.get(controlId);
	const from = prior?.lastWritten ?? readKnobValue(controlId);
	if (from === null) return null;
	const priorPartner = prior?.partnerId ?? null;
	const currentPartner = linkedPartnerId(controlId);
	let startPartner: number | null;
	if (currentPartner === null) {
		startPartner = null;
	} else if (prior !== undefined && priorPartner === currentPartner && prior.lastWrittenPartner !== null) {
		startPartner = prior.lastWrittenPartner;
	} else {
		startPartner = readKnobValue(currentPartner);
	}
	const now = _clock.now();
	const plan = buildPlan(from, now);
	if (
		!Number.isFinite(plan.startMs) ||
		!Number.isFinite(plan.endMs) ||
		plan.endMs < plan.startMs
	) {
		throw new RangeError(
			`commanded move on ${controlId}: invalid plan window ${plan.startMs}..${plan.endMs}`
		);
	}
	const move = new _Move(controlId, plan, Symbol(controlId), from, currentPartner, startPartner);
	const token = beginAnimation(controlId, label, (reason) => {
		move.stop(_outcomeForCancelReason(reason), reason);
	});
	move.token = token;
	_active.set(controlId, move);
	void _drive(move);
	return {
		get controlId() {
			return move.controlId;
		},
		get plan() {
			return move.plan;
		},
		done: move.done,
		cancel: (reason?: string) => move.cancel(reason)
	};
}

export function cancelCommandedMove(controlId: string, reason: string = 'cancelled'): boolean {
	const move = _active.get(controlId);
	if (move === undefined) return false;
	move.cancel(reason);
	return true;
}

export function commandedMoveLog(): readonly CommandedMoveRecord[] {
	return [..._log];
}

export function activeCommandedMoveIds(): string[] {
	return [..._active.keys()];
}

export function _setCommandedMoveClockForTests(clock: CommandedMoveClock | null): void {
	_clock = clock ?? _defaultClock;
}

export function _resetCommandedMovesForTests(): void {
	for (const move of _active.values()) move.cancel('test reset');
	_active.clear();
	_log.length = 0;
	_clock = _defaultClock;
	if (_handUnsub !== null) {
		_handUnsub();
		_handUnsub = null;
	}
	_handSubscribed = false;
}

async function _drive(move: _Move): Promise<void> {
	const { controlId: id } = move;
	try {
		for (;;) {
			await _clock.nextFrame();
			if (move.stopped) return;
			const t = _clock.now();
			const finished = t >= move.plan.endMs;
			const sampleAt = finished ? move.plan.endMs : Math.max(t, move.plan.startMs);
			const value = move.plan.valueAt(sampleAt);
			if (!Number.isFinite(value)) {
				throw new RangeError(`commanded move on ${id}: valueAt(${sampleAt}) is ${value}`);
			}
			if (readKnobValue(id) === null) {
				move.stop('cancelled', 'control unmounted');
				return;
			}
			_rebaselineIfLinkChanged(move);
			const written = setKnobFromBaseline(
				id,
				move.startPrimary,
				value,
				move.partnerId,
				move.startPartner
			);
			if (written === null) {
				move.stop('cancelled', 'control unmounted');
				return;
			}
			// With a link, the end primary includes stagger, exactly as a drag to that target.
			move.lastWritten = written.primary;
			move.lastWrittenPartner = written.secondary;
			if (finished) {
				_pushLog({
					control_id: id,
					planned_end_ms: move.plan.endMs,
					actual_end_ms: t
				});
				move.stop('completed', null);
				return;
			}
		}
	} catch (err) {
		move.fail(err);
	} finally {
		endAnimation(id, move.token);
		if (_active.get(id) === move) _active.delete(id);
	}
}
