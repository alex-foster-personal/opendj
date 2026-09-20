/**
 * The agent-readable audio-health block of the UI mirror.
 *
 * WHY THIS EXISTS (Thu 10 Sep 2026). The app went silent. The operator saw
 * several error toasts and the output-health bar on screen. The mirror
 * published `toasts: []` and no audio health at all, so an agent reading it
 * minutes later concluded nothing was wrong, and went looking at `master.rms`,
 * which is measured upstream of the master mute and the destination and was
 * therefore reporting a healthy 0.24 through a total outage.
 *
 * Two distinct defects produced that, and this module closes both:
 *
 *   1. EPHEMERAL. `ui-mirror.ts` mapped the LIVE toast store. Toasts dismiss
 *      after about 5 s; the mirror publishes every 1000 ms. Any fault older
 *      than a few seconds was gone. `recent_faults` reads the DURABLE perf
 *      event ring instead, so a fault is still legible long afterwards. This
 *      is the same move `autoplay_stall` already made for the same reason.
 *
 *   2. MISSING. `audioOutputHealth.snapshot` is the exact reading behind the
 *      `output-health-bar` a human can see at `TopBar.svelte:605`, and it was
 *      never mirrored. AGENT-02 requires the mirror to carry what a human
 *      reads off the screen, not a subset, so its absence was a parity defect
 *      as well as the missing diagnosis.
 *
 * A PURE FOLD, taking its clock and its inputs from the caller, for the same
 * reason `silence-watchdog.ts` and `audio-output-health-display.ts` are pure:
 * the thing worth testing is the decision, and a decision observable only by
 * waiting out a real timer is one nobody can test on a thrashing laptop.
 */
import type { AudioOutputHealthSnapshot } from './audio-output-health.svelte';
import { describeAudioOutputHealth, type AudioOutputHealthDisplay } from './audio-output-health-display';
import type { DeviceDeliverySnapshot } from './device-output-delivering';
import { audioHealthFaultSeverity, type PerfEvent } from './perf-event-log';
import type { SilenceVerdict } from './silence-watchdog';

/**
 * Above this age, a master-RMS reading is not evidence of live signal.
 *
 * The RAF loop that writes it self-terminates when nothing is transporting
 * (`audio-engine.svelte.ts:1857`) while the mirror keeps re-publishing the last
 * value, so a stale reading and a live one are otherwise indistinguishable.
 * Two mirror publishes; anything older means the writer stopped.
 */
export const METER_FRESH_MAX_MS = 2_000;

/** Most recent faults carried. Enough for a timeline, bounded for the payload. */
export const RECENT_FAULT_LIMIT = 10;

/**
 * Which rows are audio-health FAULTS is decided by `audioHealthFaultSeverity`
 * in `perf-event-log.ts`, and imported rather than restated here.
 *
 * That predicate also owns those rows' RETENTION BUDGET in the ring. A second
 * copy of the rule here would let the rows the ring keeps and the rows this
 * fold looks for drift apart, and a fault that was retained but not selected
 * (or selected but already evicted) is the same invisible-outage defect this
 * module exists to close, one level up.
 */

export interface AudioHealthFault {
	t: string;
	kind: string;
	message: string;
	age_ms: number;
	/**
	 * The severity the recording site judged. `unknown` is a row written before
	 * severity was persisted: it is carried rather than dropped, and labelled
	 * rather than promoted to a diagnosed fault.
	 */
	severity: 'warn' | 'error' | 'unknown';
}

export interface AudioHealthMirror {
	/** The merged liveness reading behind the on-screen bar, or null when unbuilt. */
	output: AudioOutputHealthSnapshot | null;
	/** OS probe verdict mirrored verbatim for agent parity (issue #923). */
	device: DeviceDeliverySnapshot | null;
	/** Exactly the class and hover text the human sees, so the two cannot disagree. */
	display: AudioOutputHealthDisplay;
	meter: { rms: number | null; age_ms: number | null; fresh: boolean };
	silence_verdict: SilenceVerdict;
	recent_faults: AudioHealthFault[];
}

export interface AudioHealthInput {
	snapshot: AudioOutputHealthSnapshot | null;
	rms: number | null;
	rmsAgeMs: number | null;
	silenceVerdict: SilenceVerdict;
	events: readonly PerfEvent[];
	nowMs: number;
}

export function buildAudioHealthMirror(input: AudioHealthInput): AudioHealthMirror {
	const { snapshot, rms, rmsAgeMs, silenceVerdict, events, nowMs } = input;

	const faults: AudioHealthFault[] = [];
	for (const event of events) {
		const severity = audioHealthFaultSeverity(event);
		if (severity === null) continue;
		const age = nowMs - Date.parse(event.t);
		if (Number.isNaN(age)) {
			throw new RangeError(`perf event ${event.kind} has an unparseable timestamp ${event.t}`);
		}
		// A negative age means a clock moved backwards under us. Reporting it as
		// a small positive number would quietly reorder the timeline an operator
		// is about to read, so it fails loudly instead.
		if (age < 0) {
			throw new RangeError(
				`perf event ${event.kind} is stamped in the future (${event.t}, ${-age}ms ahead); ` +
					'the fault timeline cannot be ordered'
			);
		}
		faults.push({ t: event.t, kind: event.kind, message: event.message, age_ms: age, severity });
	}
	faults.sort((a, b) => a.age_ms - b.age_ms);

	return {
		output: snapshot,
		device: snapshot?.device ?? null,
		display: describeAudioOutputHealth(snapshot),
		meter: {
			rms,
			age_ms: rmsAgeMs,
			// Unknown age is NOT fresh. An absent measurement must never render
			// as a good one.
			fresh: rmsAgeMs !== null && rmsAgeMs <= METER_FRESH_MAX_MS
		},
		silence_verdict: silenceVerdict,
		recent_faults: faults.slice(0, RECENT_FAULT_LIMIT)
	};
}
