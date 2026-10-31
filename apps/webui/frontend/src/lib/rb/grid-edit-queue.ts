/**
 * Deck grid adjust / grid shift: the ordered request half (GRIDEDIT-6235, -6235d).
 *
 * One queue per track. Presses run strictly in order, so a reset never lands
 * between two nudges it was pressed after; a nudge pressed while an earlier
 * one is still in flight is MERGED into the next request rather than sent
 * on its own, so a held grid-adjust button costs one request per round trip,
 * not one per 80 ms repeat, and the sum the server applies is exactly the
 * sum of the presses (the server rounds to the same microsecond).
 *
 * When the queue drains, the track's grid is re-read once (`refresh`) so
 * every deck holding it moves to the edited grid. Each press's promise
 * settles when its request (merged or not) and that re-read have: it rejects
 * with the failure, so the press's performance command reports it through
 * the dispatcher like any other deck control. A failure DROPS the rest of the
 * queue, rejecting those presses with the same error: those steps were
 * relative to a state the engine refused, and replaying them would land the
 * grid somewhere nobody pressed for.
 *
 * `createEventRefresher` turns `library.changed` (kind beatgrid_edit) into
 * re-reads. It never guesses which event is our own echo: a time window did,
 * and silently dropped an agent's edit that landed just after a local press.
 * Instead every event is honored, coalesced: one waiting for a busy queue
 * re-reads after the drain, and one arriving during a re-read schedules
 * exactly one more, since that re-read may have read the grid before the
 * write the event announces; one arriving before that re-read is sent (the
 * same event reaching a second deck on the track) is covered by it. An echo
 * of our own write costs one extra /anlz read, which the anlz cache dedupes
 * without republishing the deck.
 *
 * Ports, not imports: the deck wires the real HTTP routes and the anlz cache;
 * tests wire fakes and a fake clock.
 */

import { mergeNudges, type AnlzBeatgridSource, type GridEditIntent } from './grid-edit';

export interface GridEditValue {
	source: AnlzBeatgridSource;
	offset_ms: number;
	beat_shift: number;
}

export interface GridEditPorts {
	nudge(
		stableId: string,
		source: AnlzBeatgridSource,
		step: { offset_ms: number; beats: number }
	): Promise<GridEditValue>;
	/** Reset one half; the server keeps the other in one transaction. */
	reset(stableId: string, source: AnlzBeatgridSource, part: 'adjust' | 'shift'): Promise<GridEditValue>;
	/** Re-read the track's grid into every deck holding it. */
	refresh(stableId: string): Promise<void>;
}

interface _Waiter {
	resolve(): void;
	reject(error: Error): void;
}

/** `guard` throws when the press no longer applies (its deck changed while
 * the op waited behind another deck's edit of the same track). */
type _Op = { source: AnlzBeatgridSource; intent: GridEditIntent; waiters: _Waiter[]; guard?: (() => void) | undefined };

interface _TrackQueue {
	ops: _Op[];
	running: Promise<void> | null;
}

export interface GridEditQueue {
	/** Settles once the press has been written and re-read, or refused.
	 * `guard` runs right before the write; if it throws, this press alone is
	 * refused with that error and nothing is written for it. */
	submit(stableId: string, source: AnlzBeatgridSource, intent: GridEditIntent, guard?: () => void): Promise<void>;
	busy(stableId: string): boolean;
	/** Resolves once the track's queue has drained (tests and teardown). */
	idle(stableId: string): Promise<void>;
}

function _message(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}

export function createGridEditQueue(ports: GridEditPorts): GridEditQueue {
	const queues = new Map<string, _TrackQueue>();

	function queueFor(stableId: string): _TrackQueue {
		let queue = queues.get(stableId);
		if (queue === undefined) {
			queue = { ops: [], running: null };
			queues.set(stableId, queue);
		}
		return queue;
	}

	/** Returns whether anything was written. */
	async function execute(stableId: string, op: _Op): Promise<boolean> {
		const { intent, source } = op;
		if (intent.kind === 'nudge') {
			// Presses that cancelled out while merged (2 ms later, 2 ms earlier)
			// write nothing; the engine refuses an empty nudge.
			if (intent.offset_ms === 0 && intent.beats === 0) return false;
			await ports.nudge(stableId, source, { offset_ms: intent.offset_ms, beats: intent.beats });
			return true;
		}
		// The server keeps the other half in the same transaction, so neither an
		// earlier op in this queue nor an agent's concurrent edit of it is lost.
		await ports.reset(stableId, source, intent.part);
		return true;
	}

	async function drain(stableId: string, queue: _TrackQueue): Promise<void> {
		let wrote = false;
		// Presses whose request is done; they settle with the re-read below.
		const done: _Waiter[] = [];
		for (let op = queue.ops.shift(); op !== undefined; op = queue.ops.shift()) {
			// Another deck holding the same track can keep this queue busy
			// while this press's deck loads another track or switches grid.
			if (op.guard !== undefined) {
				try {
					op.guard();
				} catch (error) {
					const refused = error instanceof Error ? error : new Error(String(error));
					for (const waiter of op.waiters) waiter.reject(refused);
					continue;
				}
			}
			try {
				if (await execute(stableId, op)) wrote = true;
				done.push(...op.waiters);
			} catch (error) {
				const failure = new Error(`Grid edit failed - ${_message(error)}`);
				for (const dropped of [op, ...queue.ops.splice(0)]) {
					for (const waiter of dropped.waiters) waiter.reject(failure);
				}
			}
		}
		let refreshFailure: Error | null = null;
		if (wrote) {
			try {
				await ports.refresh(stableId);
			} catch (error) {
				refreshFailure = new Error(
					`Grid edit saved, but the deck could not re-read the grid - ${_message(error)}`
				);
			}
		}
		for (const waiter of done) {
			if (refreshFailure === null) waiter.resolve();
			else waiter.reject(refreshFailure);
		}
	}

	/** Runs a drain and clears `running` after it, never inside it: a drain
	 * whose presses are all refused or cancel out finishes without awaiting,
	 * so clearing from within ran BEFORE the caller stored the drain's
	 * promise, and that settled promise then stayed in `running` for good,
	 * so no later press ever started (Sol P1 BLOCKING, PR #6235). */
	function start(stableId: string, queue: _TrackQueue): void {
		const running: Promise<void> = drain(stableId, queue).finally(() => {
			if (queue.running !== running) return;
			queue.running = null;
			// A press that landed during the refresh above starts a new drain.
			if (queue.ops.length > 0) start(stableId, queue);
		});
		queue.running = running;
	}

	return {
		submit(stableId, source, intent, guard) {
			return new Promise<void>((resolve, reject) => {
				const waiter = { resolve, reject };
				const queue = queueFor(stableId);
				const last = queue.ops.at(-1);
				const merged =
					intent.kind === 'nudge' && last !== undefined && last.intent.kind === 'nudge'
						? mergeNudges(last.intent, intent)
						: null;
				if (
					merged !== null &&
					intent.kind === 'nudge' &&
					last !== undefined &&
					last.intent.kind === 'nudge' &&
					last.source === source &&
					// A guarded press belongs to one deck's target; never fold it
					// into another press it could outlive or be refused apart from.
					last.guard === undefined &&
					guard === undefined
				) {
					last.intent = { kind: 'nudge', ...merged };
					last.waiters.push(waiter);
				} else {
					queue.ops.push({ source, intent, waiters: [waiter], guard });
				}
				if (queue.running === null) start(stableId, queue);
			});
		},
		busy(stableId) {
			const queue = queues.get(stableId);
			return queue !== undefined && (queue.running !== null || queue.ops.length > 0);
		},
		async idle(stableId) {
			for (let running = queues.get(stableId)?.running; running; running = queues.get(stableId)?.running) {
				await running;
			}
		}
	};
}

export interface EventRefresher {
	/** A beatgrid_edit event named this track. */
	onEvent(stableId: string): void;
	/** Resolves once no re-read is running or owed for the track. */
	idle(stableId: string): Promise<void>;
}

/** Coalesced re-reads for grid-edit events; see the module comment. */
export function createEventRefresher(
	queue: Pick<GridEditQueue, 'idle'>,
	refresh: (stableId: string) => Promise<void>,
	reportError: (message: string) => void
): EventRefresher {
	const running = new Map<string, Promise<void>>();
	const owed = new Set<string>();
	// Tracks whose re-read request is out: only an event during one can
	// announce a write it missed. One before it (another deck holding the same
	// track, handed the same event) is covered by the read still to come.
	const reading = new Set<string>();

	async function run(stableId: string): Promise<void> {
		try {
			do {
				owed.delete(stableId);
				// The drain re-reads when it ends, but an event that arrived
				// mid-drain may announce a write that drain's re-read missed.
				await queue.idle(stableId);
				reading.add(stableId);
				try {
					await refresh(stableId);
				} catch (error) {
					reportError(`Grid was edited elsewhere, but this deck could not re-read it - ${_message(error)}`);
				} finally {
					reading.delete(stableId);
				}
			} while (owed.has(stableId));
		} finally {
			running.delete(stableId);
		}
	}

	return {
		onEvent(stableId) {
			if (running.has(stableId)) {
				if (reading.has(stableId)) owed.add(stableId);
				return;
			}
			running.set(stableId, run(stableId));
		},
		async idle(stableId) {
			for (let current = running.get(stableId); current; current = running.get(stableId)) {
				await current;
			}
		}
	};
}
