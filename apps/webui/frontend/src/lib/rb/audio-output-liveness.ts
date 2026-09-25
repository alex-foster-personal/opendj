/**
 * Detect an AudioContext that renders into a dead output device, and say so.
 *
 * Wed 2 Sep 2026 18:33 CEST, the maintainer: "can't hear audio in opendj chrome - should
 * be error state - currently happens silently." The app's context was
 * `running`, the deck was playing, the HAL position was advancing, the mixer
 * was open, the silence watchdog saw signal in the graph, and nothing came out
 * of the headphones. A FRESH context created in the same tab played a tone with
 * a 192 ms output latency; the app's context had stamped `output_latency_ms=0`
 * and never re-measured. In Chromium `AudioContext.outputLatency` is the
 * device's own figure: it is 0 for a fraction of a second after resume, and it
 * is 0 for as long as the context is bound to an output that no longer exists.
 *
 * So: a context that has been running with a deck playing for longer than the
 * grace period and still reports `outputLatency === 0` is silent, whatever the
 * graph says. That is an error state, shown as one, with an automatic re-bind
 * request and an escalation if the re-bind does not restore a device latency.
 *
 * Fri 11 Sep 2026 (issue #2155): a non-zero latency with a frozen
 * `getOutputTimestamp().contextTime` is also silent. That path now promotes to
 * `stalled` after 2 s and triggers suspend/resume plus graph recreate.
 */

import { noteOutputStall } from '$lib/rb/audio-output-rebind';

export const LIVENESS_POLL_MS = 2_500;
/** Polls with a zero latency while playing before the first alarm (2 x 2.5 s). */
export const LIVENESS_DEAD_POLLS = 2;
/** Polls after the alarm before the sticky "reload" escalation (2 more). */
export const LIVENESS_ESCALATE_POLLS = 4;
/** Frozen device output position while playing before the stalled verdict. */
export const OUTPUT_STALL_MS = 2_000;

export interface LivenessAudioContext {
	readonly state: string;
	readonly outputLatency: number;
	readonly baseLatency: number;
	readonly sinkId?: unknown;
	getOutputTimestamp(): { contextTime?: number; performanceTime?: number };
}

export interface LivenessEffects {
	pushToast(message: string, kind: 'info' | 'error'): void;
	recordPerfEvent(kind: string, message: string, severity: 'info' | 'error'): void;
	setInterval(fn: () => void, ms: number): unknown;
	clearInterval(handle: unknown): void;
	now?: () => number;
	recoverOutput?(): void;
	/**
	 * Re-bind a context whose output went dead. Omitted = the MASTER context,
	 * which re-binds through the global `noteOutputStall` fan-out. Any other
	 * context (the headphone cue context) must pass its own, or a dead headphone
	 * leg would suspend and resume the room.
	 */
	rebindDeadOutput?(): void;
	/**
	 * Fired after EVERY poll (idle included), not only on a verdict change, so a
	 * UI indicator (pin 93c82bb36eb7: the 1px bar under master volume) can show
	 * the live verdict rather than only the toast trail of past transitions.
	 * Optional so the many existing test harnesses need no update.
	 */
	onSnapshot?(snapshot: AudioOutputSnapshot): void;
}

export type LivenessVerdict = 'idle' | 'ok' | 'stalled' | 'dead' | 'dead-escalated';

export interface AudioOutputSnapshot {
	state: string;
	output_latency_ms: number;
	base_latency_ms: number;
	sink_id: unknown;
	output_context_time_s: number | null;
	verdict: LivenessVerdict;
}

export function snapshotAudioOutput(ctx: LivenessAudioContext, verdict: LivenessVerdict): AudioOutputSnapshot {
	const ts = ctx.getOutputTimestamp();
	return {
		state: ctx.state,
		output_latency_ms: Math.round(ctx.outputLatency * 1e6) / 1000,
		base_latency_ms: Math.round(ctx.baseLatency * 1e6) / 1000,
		sink_id: ctx.sinkId,
		output_context_time_s: typeof ts.contextTime === 'number' ? ts.contextTime : null,
		verdict
	};
}

function _outputTimestampAdvancing(contextTime: number, previousContextTime: number | null): boolean {
	return contextTime > 0 && (previousContextTime === null || contextTime > previousContextTime);
}

export function installOutputLiveness(
	ctx: LivenessAudioContext,
	effects: LivenessEffects,
	isAnyDeckPlaying: () => boolean
): { verdict(): LivenessVerdict; snapshot(): AudioOutputSnapshot; uninstall(): void } {
	let deadPolls = 0;
	let verdict: LivenessVerdict = 'idle';
	let lastContextTime: number | null = null;
	let lastAdvanceAtMs = 0;
	let stallEdgeReported = false;
	const now = (): number => effects.now?.() ?? Date.now();

	function tick(): void {
		tickVerdict();
		effects.onSnapshot?.(snapshotAudioOutput(ctx, verdict));
	}

	function _restoreFromBroken(): void {
		if (verdict === 'dead' || verdict === 'dead-escalated' || verdict === 'stalled') {
			effects.recordPerfEvent('audio-output-alive', 'output device latency is back; audio is reaching a device again', 'info');
			effects.pushToast('Audio output restored', 'info');
		}
	}

	function tickVerdict(): void {
		if (!isAnyDeckPlaying() || ctx.state !== 'running') {
			deadPolls = 0;
			lastContextTime = null;
			lastAdvanceAtMs = 0;
			stallEdgeReported = false;
			verdict = 'idle';
			return;
		}

		const ts = ctx.getOutputTimestamp();
		const contextTime = ts.contextTime;
		if (typeof contextTime !== 'number' || !Number.isFinite(contextTime)) {
			return;
		}
		if (_outputTimestampAdvancing(contextTime, lastContextTime)) {
			if (verdict === 'stalled') _restoreFromBroken();
			lastContextTime = contextTime;
			lastAdvanceAtMs = now();
			stallEdgeReported = false;
			if (ctx.outputLatency > 0) {
				if (verdict === 'dead' || verdict === 'dead-escalated') _restoreFromBroken();
				deadPolls = 0;
				verdict = 'ok';
				return;
			}
		} else if (lastContextTime !== null) {
			const stalledForMs = now() - lastAdvanceAtMs;
			if (stalledForMs >= OUTPUT_STALL_MS) {
				verdict = 'stalled';
				if (!stallEdgeReported) {
					stallEdgeReported = true;
					effects.recordPerfEvent(
						'audio-output-stalled',
						`device output position frozen for ${Math.round(stalledForMs)}ms ` +
							`(>= ${OUTPUT_STALL_MS}ms) while a deck is playing; recovering`,
						'error'
					);
					effects.pushToast(
						'NO AUDIO OUTPUT: the device output position stalled. Recovering...',
						'error'
					);
					effects.recoverOutput?.();
				}
				return;
			}
			if (ctx.outputLatency > 0) {
				if (verdict === 'dead' || verdict === 'dead-escalated') _restoreFromBroken();
				deadPolls = 0;
				verdict = 'ok';
				return;
			}
		} else if (contextTime > 0) {
			lastContextTime = contextTime;
			lastAdvanceAtMs = now();
		}

		if (ctx.outputLatency > 0) {
			if (verdict === 'dead' || verdict === 'dead-escalated') _restoreFromBroken();
			deadPolls = 0;
			if (verdict !== 'stalled') verdict = 'ok';
			return;
		}

		deadPolls += 1;
		if (deadPolls === LIVENESS_DEAD_POLLS) {
			verdict = 'dead';
			effects.recordPerfEvent(
				'audio-output-dead',
				'context running and a deck playing, but the output reports no device latency: ' +
					'the browser is rendering into a dead output. Re-binding.',
				'error'
			);
			effects.pushToast('NO AUDIO OUTPUT: the browser is rendering into a dead device. Re-binding...', 'error');
			if (effects.rebindDeadOutput !== undefined) effects.rebindDeadOutput();
			else noteOutputStall(0);
		} else if (deadPolls === LIVENESS_ESCALATE_POLLS) {
			verdict = 'dead-escalated';
			effects.recordPerfEvent(
				'audio-output-dead-persistent',
				'the re-bind did not restore a device latency; a reload rebuilds the audio graph',
				'error'
			);
			effects.pushToast('NO AUDIO OUTPUT after re-bind. Reload the page (Cmd+R) to rebuild the audio graph.', 'error');
		}
	}

	const handle = effects.setInterval(tick, LIVENESS_POLL_MS);
	return {
		verdict: () => verdict,
		snapshot: () => snapshotAudioOutput(ctx, verdict),
		uninstall: () => effects.clearInterval(handle)
	};
}
