/**
 * The room-output reopen for a MAIN device pick (IOPIN-18). Loaded on the
 * first pick by headphones.ts, so it stays off the first-paint bundle of "/".
 * The timeout wrapper is passed in rather than imported, so this module does
 * not import headphones.ts back.
 */
import { beginIntentionalSuspend, beginLateSuspend, endIntentionalSuspend, endLateSuspend } from '$lib/rb/audio-context-watchdog';
import { recordPerfEvent } from '$lib/rb/perf-event-log';

type OperationTimeout = <T>(operation: string, promise: Promise<T>) => Promise<T>;

/** How long a timed-out suspend keeps the watchdog's excuse while it may still
 * land. A suspend that never settles must not blind the watchdog for good, so
 * the excuse ends at the first of the suspend settling or this grace ending. */
export const LATE_SUSPEND_EXCUSE_MS = 10_000;

/** Room contexts this module suspended for a reopen and has not yet resumed.
 * A pick resumes these; any other suspended context is a deliberate pause. */
const roomsSuspendedByReopen = new WeakMap<object, () => void>();

/** Mark a room the reopen is about to suspend. The mark clears the moment the
 * room is seen running again, whoever resumed it (this module, or the app on
 * Play), so it only ever covers a suspension this module made and has not
 * yet undone; a later pause by the app is never mistaken for ours. */
function markRoomSuspendedByReopen(room: AudioContext): void {
	if (roomsSuspendedByReopen.has(room)) return;
	const target = room as AudioContext & {
		addEventListener?: (type: string, listener: () => void) => void;
	};
	const onStateChange = (): void => {
		if ((room.state as string) === 'running') clearReopenMark(room);
	};
	roomsSuspendedByReopen.set(room, onStateChange);
	if (typeof target.addEventListener === 'function') target.addEventListener('statechange', onStateChange);
}

/** Reopens running on each room. A late suspend that lands with none running
 * has nobody left to undo it, so it resumes the room itself. */
const reopensInFlight = new WeakMap<object, number>();

function inFlight(room: AudioContext): number {
	return reopensInFlight.get(room) ?? 0;
}

/**
 * A timed-out suspend landed after every reopen on its room finished (a later
 * pick may already have cleared its retry error), so the room went silent with
 * nothing left to resume it. Resume it now, bounded by the same timeout; a
 * failure is recorded and leaves the mark, so the next pick still resumes it.
 */
function resumeLateLanding(room: AudioContext & { resume: () => Promise<void> }, withTimeout: OperationTimeout): void {
	void withTimeout('master reopen late resume', room.resume()).then(
		() => {
			if ((room.state as string) === 'running') clearReopenMark(room);
		},
		(error: unknown) => {
			const reason = error instanceof Error ? error.message : String(error);
			recordPerfEvent('master-reopen-late-resume-failed', `a late reopen suspend left the room suspended and its resume failed: ${reason}`, null, 'error');
		}
	);
}

function clearReopenMark(room: AudioContext): void {
	const onStateChange = roomsSuspendedByReopen.get(room);
	if (onStateChange === undefined) return;
	roomsSuspendedByReopen.delete(room);
	const target = room as AudioContext & {
		removeEventListener?: (type: string, listener: () => void) => void;
	};
	if (typeof target.removeEventListener === 'function') target.removeEventListener('statechange', onStateChange);
}

/**
 * Watch `room` for a `suspended` edge that has aged a task with the room silent
 * since. A suspend settles in the task its own edge fires (either order), so a
 * settle that finds such an edge came after something else silenced the room;
 * it did not land the silence itself. The watchdog gives that edge the same
 * one-task verdict.
 */
function watchSuspendedEdgeAging(room: AudioContext): { aged: () => boolean; stop: () => void } {
	let aged = false;
	const timers: ReturnType<typeof setTimeout>[] = [];
	const onState = (): void => {
		if ((room.state as string) === 'suspended') {
			timers.push(setTimeout(() => (aged = true), 0));
			return;
		}
		// The room ran again since: only a silence that has held since then counts.
		aged = false;
		for (const timer of timers.splice(0)) clearTimeout(timer);
	};
	const target = room as Partial<Pick<AudioContext, 'addEventListener' | 'removeEventListener'>>;
	target.addEventListener?.('statechange', onState);
	return {
		aged: () => aged,
		stop: () => {
			target.removeEventListener?.('statechange', onState);
			for (const timer of timers) clearTimeout(timer);
		}
	};
}

/**
 * A run of output switches leaves the room context `running` on a dead stream
 * (the Mac app after the OS default moves, Chrome after repeated setSinkId).
 * Play will not resume a context that still says running, so the room stays
 * silent. Suspend, then resume, so the browser reopens the stream.
 *
 * Returns null on success, else the reason. It never touches mixer state: the
 * caller publishes the reason only while its pick is still the newest one.
 *
 * Every exit leaves the room either running or with a reason to show:
 * - no suspend/resume, or closed: a failure, never a pass;
 * - suspended by THIS module (marked in roomsSuspendedByReopen) and not yet
 *   resumed, because a reopen failed or a concurrent pick is mid-reopen: it is
 *   resumed, never mistaken for a deliberate pause;
 * - suspended by anything else (the app paused it, or autoplay policy is still
 *   waiting on a gesture): left alone, since resuming would undo the pause or
 *   hang on a resume the browser will not grant yet.
 * The mark clears whenever the room is seen running (markRoomSuspendedByReopen),
 * and when a suspend fails or times out with the room still running. Any
 * suspend that fails or times out, whatever the room's state then, keeps the
 * watchdog's intentional-suspend excuse until its own promise settles (bounded
 * by LATE_SUSPEND_EXCUSE_MS, so one that never settles cannot blind the
 * watchdog; past it, the watchdog holds a `suspended` verdict one task for the
 * suspend to report it landed), and one that lands later marks the room again,
 * since that silence is ours. A late landing with no reopen left running on
 * the room resumes it at once (resumeLateLanding); one that lands while another
 * reopen runs leaves the resume to that reopen.
 */
export async function reopenRoomOutput(
	context: AudioContext,
	withTimeout: OperationTimeout,
	lateExcuseMs: number = LATE_SUSPEND_EXCUSE_MS
): Promise<string | null> {
	const room = context as AudioContext & {
		suspend?: () => Promise<void>;
		resume?: () => Promise<void>;
	};
	if (typeof room.suspend !== 'function' || typeof room.resume !== 'function') {
		return 'the room audio context cannot be suspended and resumed';
	}
	if ((room.state as string) === 'closed') return 'the room audio context is closed';
	if ((room.state as string) !== 'running' && !roomsSuspendedByReopen.has(room)) return null;
	// The watchdog must not read this suspend as audio dying mid-set.
	beginIntentionalSuspend(room);
	reopensInFlight.set(room, inFlight(room) + 1);
	// A timed-out suspend still pending keeps that excuse until it settles, so
	// its late `suspended` edge is never read as an unexpected pause.
	let excuseHeldBySuspend = false;
	try {
		if ((room.state as string) === 'running') {
			markRoomSuspendedByReopen(room);
			const suspending = room.suspend();
			// A `suspended` edge this suspend did not settle with (a task or more
			// before it): something else paused the room while it was pending,
			// so a late settle finding the room suspended is not its landing.
			const pausedElsewhere = watchSuspendedEdgeAging(room);
			try {
				await withTimeout('master reopen suspend', suspending);
				pausedElsewhere.stop();
				// Overlapping reopens share one mark, and another's resume clears it
				// when the room runs again; this suspend landing after that is ours.
				if ((room.state as string) === 'suspended') markRoomSuspendedByReopen(room);
			} catch (error) {
				// The suspend did not land: still running, so the mark covers
				// nothing of ours and must not make a later pause by the app look
				// like a failed reopen.
				if ((room.state as string) === 'running') clearReopenMark(room);
				// Whatever the room's state now (an overlapping reopen's suspend may
				// already hold it suspended, and its resume may follow), this
				// suspend is still ours until it settles: one that lands late marks
				// the room again and keeps the watchdog's excuse up to then.
				// The excuse ends once, at the first of the suspend settling or the
				// grace ending, so a suspend that never settles is bounded. Past the
				// grace it is a late suspend: the watchdog still alarms on a genuine
				// pause, but holds its verdict on a `suspended` edge until this
				// suspend can say it landed.
				excuseHeldBySuspend = true;
				let excused = true;
				let late = false;
				const endExcuse = (): void => {
					if (!excused) return;
					excused = false;
					clearTimeout(grace);
					endIntentionalSuspend(room);
				};
				const grace = setTimeout(() => {
					if (!excused) return;
					endExcuse();
					late = true;
					beginLateSuspend(room);
				}, lateExcuseMs);
				const settle = (landedHere: boolean): void => {
					if (late) endLateSuspend(room, landedHere);
					else endExcuse();
				};
				void suspending.then(
					() => {
						const landedHere = (room.state as string) === 'suspended' && !pausedElsewhere.aged();
						pausedElsewhere.stop();
						if (landedHere) markRoomSuspendedByReopen(room);
						settle(landedHere);
						if (landedHere && inFlight(room) === 0) resumeLateLanding(room as AudioContext & { resume: () => Promise<void> }, withTimeout);
					},
					() => {
						pausedElsewhere.stop();
						settle(false);
					}
				);
				throw error;
			}
		}
		if ((room.state as string) === 'closed') return 'the room audio context is closed';
		await withTimeout('master reopen resume', room.resume());
		clearReopenMark(room);
		return null;
	} catch (error) {
		return error instanceof Error ? error.message : String(error);
	} finally {
		if (!excuseHeldBySuspend) endIntentionalSuspend(room);
		const running = inFlight(room) - 1;
		if (running > 0) reopensInFlight.set(room, running);
		else reopensInFlight.delete(room);
	}
}
