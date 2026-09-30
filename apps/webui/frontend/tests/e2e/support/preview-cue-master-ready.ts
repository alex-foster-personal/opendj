import { AUDIO_CONTEXT_IO_TIMEOUT_MS } from '../../../src/lib/rb/audio-context-io-timeout';
import { CONTEXT_RESUME_BACKOFF_MS } from '../../../src/lib/rb/audio-context-watchdog';

/**
 * #4140-class flake: `play()` can hand a deck an AudioContext that is still
 * `suspended` (device contention under CI load - see
 * `audio-context-watchdog.ts`'s Wed 2 Sep 2026 postmortem), and a deck's
 * `position_ms` does not move until `_publishPresentedTransport` starts
 * receiving real output timestamps from a `running` context. On run
 * 36331906929 (Sun 27 Sep 2026, mergify batch #4140) the job log shows
 * exactly this for deck 1 - `audio-unexpected-pause: cause=context-suspended
 * deck=1 position_ms=124` - and the failure snapshot still reads
 * `playing`/`is_master` true with elapsed time frozen near that same ~100ms,
 * i.e. the transport never resumed publishing before
 * `preview-cue-library.spec.ts`'s old fixed 30s poll gave up.
 *
 * The engine's own watchdog (`audio-context-watchdog.ts`) recovers a
 * suspended context on a bounded schedule - every delay in
 * `CONTEXT_RESUME_BACKOFF_MS` plus one `AUDIO_CONTEXT_IO_TIMEOUT_MS` per
 * attempt - and is RE-ARMED (not extended) on each new recovery opportunity,
 * so a single transient hiccup can cost up to two such windows before the
 * context is reliably back. A fixed `30_000` has no relation to that
 * schedule and can time out mid-recovery under load; bumping it to a bigger
 * arbitrary number would only move the race, not close it.
 *
 * So the deadline for "deck N is playing, master and moving" is derived from
 * the live product constants - two full recovery windows plus margin for the
 * worklet to publish its first sample after `running` - rather than picked
 * by hand. A context still not publishing after two of its own recovery
 * windows is a product bug for the watchdog to answer for, not something
 * this deadline should paper over by growing further.
 *
 * `tests/unit/preview-cue-master-ready-deadline.test.mjs` pins that this
 * deadline tracks the live constants (mirrors
 * `destination-tap-deadline.test.mjs`'s pattern for the same class of wait).
 */
export function watchdogRecoveryWindowMs(
	backoffMs: readonly number[] = CONTEXT_RESUME_BACKOFF_MS,
	ioTimeoutMs: number = AUDIO_CONTEXT_IO_TIMEOUT_MS
): number {
	return backoffMs.reduce((total, delay) => total + delay, 0) + backoffMs.length * ioTimeoutMs;
}

/** Margin for the worklet to publish its first post-recovery sample. */
const PUBLISH_MARGIN_MS = 5_000;

export const MASTER_READY_TIMEOUT_MS = 2 * watchdogRecoveryWindowMs() + PUBLISH_MARGIN_MS;
