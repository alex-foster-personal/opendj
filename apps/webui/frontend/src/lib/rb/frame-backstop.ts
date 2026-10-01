/**
 * Keeps deck state moving when the browser stops delivering animation frames.
 *
 * requirement: AUDIOLIVE-11
 *
 * A browser stops firing requestAnimationFrame for a hidden, minimized or
 * occluded page while the AudioContext keeps rendering. The engine's
 * presentation loop is a frame loop, and it publishes far more than pixels:
 * `audible`, `transport_pending`, the position mirror, the natural-end stop,
 * the safety-loop re-arm and Beat Sync phase correction all advance inside it.
 * With frames gone, a paused deck stayed `audible` for ever, `anyDeckPlaying()`
 * never cleared, and `unload()` awaited a frame that never came.
 *
 * Frames stay the clock for painting. This module adds the three clocks that
 * do not stop with them, in order of preference:
 *
 * - the AUDIO clock: an `ended` event from a muted source stopped at a context
 *   time. It is dispatched for audio time, not for a timer, so page timer
 *   throttling (1 s when hidden, up to 1 min under intensive throttling) does
 *   not delay it. It needs a running context.
 * - a TIMER: covers a context that is not running. Throttled when hidden, so it
 *   bounds latency rather than setting it.
 * - the frame itself, when it does fire.
 *
 * A Worker timer would also escape page throttling; it is not used because the
 * audio event already does and is tied to the clock the state is derived from.
 *
 * Regression lines:
 * - if a pending frame that never fires is not run by the backstop then a
 *   hidden tab freezes deck state [⛔️]
 * - if the backstop runs while frames are firing then the presentation rate
 *   and the audio-Hz meter inflate [⛔️]
 * - if a presented-stop wait can outlive its deadline without a named error
 *   then unload hangs silently [⛔️]
 */

/** How long one frame may stay pending before it counts as stalled. Six frames
 * at 60 Hz: long enough that a janky visible tab never trips it, short enough
 * that hidden-tab state trails the output by less than a beat. */
const FRAME_BACKSTOP_MS = 100;
/** Margin past the computed presentation instant, covering the granularity of
 * the output timestamp. */
const PRESENTED_MARGIN_SEC = 0.03;
/** Poll step of the presented-stop wait, on the audio clock. */
const PRESENTED_STOP_STEP_MS = 20;

interface BackstopTimers {
	set(run: () => void, ms: number): unknown;
	clear(handle: unknown): void;
}

const REAL_TIMERS: BackstopTimers = {
	set: (run, ms) => setTimeout(run, ms),
	clear: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>)
};

/** The slice of AudioContext the audio-clock wake needs. */
interface AudioClock {
	readonly currentTime: number;
	readonly state: string;
	readonly destination: AudioNode;
	createConstantSource(): ConstantSourceNode;
	createGain(): GainNode;
	getOutputTimestamp?(): { contextTime?: number };
}

//-----------------------------------------------------------------------------
// audio-clock wake
//-----------------------------------------------------------------------------

/** Run `run` when the audio clock reaches `when`. Silent: the source feeds a
 * zero gain, which exists only so the context renders the source at all. */
function atContextTime(ctx: AudioClock, when: number, run: () => void): void {
	const source = ctx.createConstantSource();
	const mute = ctx.createGain();
	mute.gain.value = 0;
	source.connect(mute).connect(ctx.destination);
	source.onended = () => {
		source.disconnect();
		mute.disconnect();
		run();
	};
	source.start();
	source.stop(Math.max(when, ctx.currentTime));
}

/** Resolve after `ms` of audio time or `ms` of timer time, whichever fires
 * first. Never waits on a frame. */
export function audioClockSleep(
	ctx: AudioClock | null,
	ms: number,
	timers: BackstopTimers = REAL_TIMERS
): Promise<void> {
	return new Promise((resolve) => {
		const timer = timers.set(resolve, ms);
		if (ctx === null || ctx.state !== 'running') return;
		atContextTime(ctx, ctx.currentTime + ms / 1000, () => {
			timers.clear(timer);
			resolve();
		});
	});
}

/** Resolve on the next frame, or after `ms` when no frame arrives. For waits
 * whose pace is a frame while visible and which must still finish hidden. */
export function frameOrTimeout(ms: number = FRAME_BACKSTOP_MS): Promise<void> {
	return new Promise((resolve) => {
		const timer = setTimeout(resolve, ms);
		requestAnimationFrame(() => {
			clearTimeout(timer);
			resolve();
		});
	});
}

//-----------------------------------------------------------------------------
// presented-stop wait
//-----------------------------------------------------------------------------

export class PresentedStopTimeoutError extends Error {
	override name = 'PresentedStopTimeoutError';
}

/** Wait until `stopped()` holds, pacing on the audio clock. `stopped` must
 * publish fresh state itself; nothing here assumes a frame loop is running.
 * Rejects with PresentedStopTimeoutError at the deadline. */
export async function awaitPresentedStop(
	stopped: () => boolean,
	ctx: AudioClock | null,
	timeoutMs = 2000,
	now: () => number = () => performance.now(),
	sleep: (ctx: AudioClock | null, ms: number) => Promise<void> = audioClockSleep
): Promise<void> {
	const deadline = now() + timeoutMs;
	while (!stopped()) {
		if (now() >= deadline) {
			throw new PresentedStopTimeoutError(
				`transport did not reach a presented stop within ${timeoutMs} ms`
			);
		}
		await sleep(ctx, PRESENTED_STOP_STEP_MS);
	}
}

//-----------------------------------------------------------------------------
// frame backstop
//-----------------------------------------------------------------------------

interface FrameBackstop {
	/** Watch the pending frame. Idempotent; a no-op when no frame is pending. */
	arm(): void;
	/** Schedule one audio-clock run for a transport change that reaches the
	 * output at render time `when`. Only while frames are not being delivered. */
	wake(ctx: AudioClock, when: number): void;
	cancel(): void;
}

/**
 * Watches a frame loop and runs it from a timer when its frame stops firing.
 *
 * `pendingFrame` returns the loop's pending frame handle, or null when the loop
 * is idle. A handle that is still the pending one a whole interval later was
 * never delivered, so `runStalled` runs the loop body instead; it must cancel
 * that frame first so the loop cannot double up when frames resume.
 */
export function createFrameBackstop(
	pendingFrame: () => unknown,
	runStalled: () => void,
	intervalMs: number = FRAME_BACKSTOP_MS,
	timers: BackstopTimers = REAL_TIMERS,
	pageHidden: () => boolean = () => typeof document !== 'undefined' && document.hidden
): FrameBackstop {
	let armed = false;
	let timer: unknown = null;
	let watched: unknown = null;
	let stalled = false;
	const arm = (): void => {
		if (armed) return;
		watched = pendingFrame();
		if (watched === null) return;
		armed = true;
		timer = timers.set(() => {
			armed = false;
			const frame = pendingFrame();
			stalled = frame !== null && frame === watched;
			if (stalled) runStalled();
			arm();
		}, intervalMs);
	};
	return {
		arm,
		wake(ctx, when) {
			if (ctx.state !== 'running' || (!stalled && !pageHidden())) return;
			const output = ctx.getOutputTimestamp?.().contextTime;
			const lagSec = output === undefined ? 0 : Math.max(0, ctx.currentTime - output);
			atContextTime(ctx, when + lagSec + PRESENTED_MARGIN_SEC, runStalled);
		},
		cancel() {
			if (armed) timers.clear(timer);
			armed = false;
			stalled = false;
		}
	};
}
