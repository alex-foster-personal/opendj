/**
 * AGENT-30: the bus side of a step set scheduled on the audio clock (`at_context_sec`).
 *
 * - A scheduled time must be a short way ahead (`MAX_SCHEDULE_AHEAD_S`), so a stale or malformed order can never
 *   leave a change armed to fire during a later, unrelated transition (Sol P2 on #7594).
 * - The mixer MIRROR and everything that reads it (the level handoff, a plan's own guards) take the new value only
 *   when the step lands, not when it is sent (Sol P1 on #7594): until then the room still hears the old one. A plain
 *   set on the same control cancels every write still waiting, as it cancels the AudioParam automation.
 */

/** The plan executor sends at most 100 ms ahead; one second leaves room for timer jitter and nothing more. */
export const MAX_SCHEDULE_AHEAD_S = 1.0;

/** When a step starts on the AudioContext clock: `at` (never in the past), or `now` for a plain set. */
export function scheduledStartSec(at: number | undefined, now: number): number {
	if (at === undefined) return now;
	if (!Number.isFinite(at)) throw new RangeError(`at_context_sec ${at} is not a time`);
	if (at - now > MAX_SCHEDULE_AHEAD_S) {
		throw new RangeError(
			`at_context_sec ${at} is ${(at - now).toFixed(3)} s ahead; at most ${MAX_SCHEDULE_AHEAD_S} s is allowed`
		);
	}
	return Math.max(at, now);
}

/**
 * Mirror writes keyed by control: applied now (a plain set), or when their scheduled step lands on the AUDIO clock.
 *
 * - Each control has ONE queue, ordered by AudioContext time, and ONE wake-up timer for its earliest write. A wake
 *   drains every write the audio clock has reached, in time order, so the mirror always ends on the latest landed
 *   step whatever order timers fire in (Sol P1 #7594), and waits again while the context has not reached the next one
 *   (a suspended context never moves the mirror ahead of the room, Codex BLOCKING #7594).
 * - A plain set cancels every write still waiting on its control, as it cancels the automation.
 */
interface _QueuedWrite {
	at: number;
	apply: () => void;
	onCancel: () => void;
}

export class DeferredMirrorWrites {
	readonly #queues = new Map<string, { writes: _QueuedWrite[]; timer: unknown }>();

	constructor(
		private readonly setTimer: (callback: () => void, ms: number) => unknown,
		private readonly clearTimer: (handle: unknown) => void,
		/** The AudioContext clock in seconds, or null once there is no context (its automation is gone with it). */
		private readonly contextNowSec: () => number | null
	) {}

	write(
		key: string,
		atContextSec: number | null,
		apply: () => void,
		onCancel: () => void = () => {}
	): void {
		if (atContextSec === null) {
			this.cancel(key);
			apply();
			return;
		}
		const queue = this.#queues.get(key) ?? { writes: [], timer: null };
		this.#queues.set(key, queue);
		queue.writes.push({ at: atContextSec, apply, onCancel });
		queue.writes.sort((a, b) => a.at - b.at);
		this.#arm(key);
	}

	#arm(key: string): void {
		const queue = this.#queues.get(key);
		if (queue === undefined) return;
		if (queue.timer !== null) this.clearTimer(queue.timer);
		queue.timer = null;
		if (queue.writes.length === 0) {
			this.#queues.delete(key);
			return;
		}
		const now = this.contextNowSec();
		const wait = now === null ? 0 : Math.max(0, (queue.writes[0].at - now) * 1000);
		queue.timer = this.setTimer(() => this.#drain(key), wait);
	}

	#drain(key: string): void {
		const queue = this.#queues.get(key);
		if (queue === undefined) return;
		queue.timer = null;
		const now = this.contextNowSec();
		if (now === null) {
			this.cancel(key); // no context: its automation is gone, so nothing lands
			return;
		}
		while (queue.writes.length > 0 && queue.writes[0].at <= now) queue.writes.shift()!.apply();
		this.#arm(key);
	}

	/** The latest time a write still waiting on `key` lands at, or null. */
	latestWaiting(key: string): number | null {
		const writes = this.#queues.get(key)?.writes ?? [];
		return writes.length === 0 ? null : writes[writes.length - 1].at;
	}

	/** Drop every write still waiting on `key` (each told through its onCancel); true when there was one. */
	cancel(key: string): boolean {
		const queue = this.#queues.get(key);
		if (queue === undefined) return false;
		if (queue.timer !== null) this.clearTimer(queue.timer);
		this.#queues.delete(key);
		for (const write of queue.writes) write.onCancel();
		return queue.writes.length > 0;
	}

	/** Drop every write still waiting (Codex BLOCKING #7594): the owner is torn down, so its automation is gone and a
	 * landing write would mutate the next session's mirror. */
	cancelAll(): void {
		for (const key of [...this.#queues.keys()]) this.cancel(key);
	}
}

/** A scheduled step's state, owned by the engine: the plan reads it rather than inferring it from the mirror. */
/**
 * A scheduled step's state, owned by the engine: the plan reads it rather than inferring it from the mirror.
 * `lost`: the engine session (graph context) ended while the step waited, so its automation is gone but nobody chose
 * to drop it, unlike `cancelled` (a plain set on its control).
 */
export type ScheduledStepState = 'waiting' | 'landed' | 'cancelled' | 'lost';

/**
 * The engine side of a scheduled step set, kept out of the engine (its fan-out and size sit at the ratchet ceilings):
 * the audio change starts on its AudioContext time and the mirror write waits for it. Each step carries the caller's
 * id and an EXPLICIT state (Sol P1 #7594): `waiting` until its mirror write applies on the audio clock (`landed`), or
 * `cancelled` the moment a plain set on that control, or engine teardown, drops it. Nothing about it is inferred from
 * the mirror's value or from elapsed time.
 */
export class ScheduledSteps {
	readonly #writes: DeferredMirrorWrites;
	readonly #states = new Map<string, ScheduledStepState>();

	constructor(
		private readonly contextNowSec: () => number | null,
		setTimer: (callback: () => void, ms: number) => unknown = (callback, ms) =>
			setTimeout(callback, ms),
		clearTimer: (handle: unknown) => void = (handle) =>
			clearTimeout(handle as ReturnType<typeof setTimeout>)
	) {
		this.#writes = new DeferredMirrorWrites(setTimer, clearTimer, contextNowSec);
	}

	/** Start `applyAudio` at the step's (bounded) time, and `applyMirror` once the audio clock reaches it. */
	schedule(
		stepId: string,
		key: string,
		atContextSec: number,
		applyAudio: (startSec: number) => void,
		applyMirror: () => void
	): void {
		if (stepId === '') throw new TypeError('a scheduled step needs a step id');
		// An id is unique while its step waits; a finished one (landed, cancelled, lost) may be scheduled again.
		if (this.#states.get(stepId) === 'waiting')
			throw new Error(`step ${stepId} is already scheduled and waiting`);
		const now = this.contextNowSec();
		if (now === null) throw new Error('audio graph not initialised');
		const start = scheduledStartSec(atContextSec, now);
		// Codex BLOCKING #7594: a step that starts before one still waiting on its control would cancel (EQ) or reorder
		// that one's automation while its mirror write still lands later, so the mirror would show a step never heard.
		// Steps on one control are scheduled in time order; anything else is refused, never half-applied.
		const latest = this.#writes.latestWaiting(key);
		if (latest !== null && start < latest) {
			throw new RangeError(
				`${key}: a step at ${start.toFixed(3)} s is earlier than one still waiting at ${latest.toFixed(3)} s`
			);
		}
		applyAudio(start);
		this.#states.set(stepId, 'waiting');
		this.#writes.write(
			key,
			start,
			() => {
				applyMirror();
				this.#states.set(stepId, 'landed');
			},
			() => this.#states.set(stepId, 'cancelled')
		);
	}

	/** The step's state, or null when this engine never scheduled it (a remount) or its runner already forgot it. */
	state(stepId: string): ScheduledStepState | null {
		return this.#states.get(stepId) ?? null;
	}

	/** A plain set on `key`: cancels its waiting steps and, when there were any, the param's automation; true when any waited. */
	cancelPlain(key: string, param: AudioParam | null): boolean {
		const now = this.contextNowSec();
		const cancelled = this.#writes.cancel(key);
		if (cancelled && param !== null && now !== null) param.cancelScheduledValues(now);
		return cancelled;
	}

	/** The engine session ends (dispose or a graph rebuild): every waiting step is `lost`; finished states are kept, so a
	 * reader can still tell a landed or cancelled step from one the session took with it (Codex #7594). */
	cancelAll(): void {
		const waiting = [...this.#states].filter(([, state]) => state === 'waiting').map(([id]) => id);
		this.#writes.cancelAll();
		for (const id of waiting) this.#states.set(id, 'lost');
	}

	/** The runner that scheduled a finished step has retired it: only then is its state dropped (Sol P1 #7594: a cap
	 * evicted landed states before the runner read them, and the null it then read replayed the step as lost). A waiting
	 * step is never forgotten. */
	forget(stepId: string): void {
		if (this.#states.get(stepId) !== 'waiting') this.#states.delete(stepId);
	}
}
