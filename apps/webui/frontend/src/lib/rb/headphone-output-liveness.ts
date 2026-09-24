/**
 * Headphone (cue) output liveness, split from the master-output detector in
 * audio-output-liveness.ts. Same poll, stall and dead timing, applied to the
 * headphone path: the cue bridge's own AudioContext plus its underrun count,
 * and the older detached media-element sink kept for its unit harness.
 */

import {
	LIVENESS_DEAD_POLLS,
	LIVENESS_ESCALATE_POLLS,
	LIVENESS_POLL_MS,
	OUTPUT_STALL_MS,
	installOutputLiveness,
	snapshotAudioOutput,
	type AudioOutputSnapshot,
	type LivenessAudioContext,
	type LivenessEffects,
	type LivenessVerdict
} from '$lib/rb/audio-output-liveness';

/** `HTMLMediaElement.readyState` value that means current playback data exists. */
export const HTML_MEDIA_HAVE_CURRENT_DATA = 2;

export interface HeadphoneLivenessElement {
	readonly currentTime: number;
	readonly readyState: number;
	readonly paused: boolean;
	readonly ended: boolean;
	readonly error: MediaError | null;
	addEventListener(type: string, listener: () => void): void;
	removeEventListener(type: string, listener: () => void): void;
}

export interface HeadphoneLivenessEffects {
	pushToast(message: string, kind: 'info' | 'error'): void;
	recordPerfEvent(kind: string, message: string, severity: 'info' | 'error'): void;
	setInterval(fn: () => void, ms: number): unknown;
	clearInterval(handle: unknown): void;
	now?: () => number;
	onSnapshot?(snapshot: HeadphoneMediaOutputSnapshot): void;
	/** Subscribe to `navigator.mediaDevices` devicechange; return an unsubscribe. */
	watchDeviceChanges?: ((handler: () => void) => () => void) | undefined;
}

/** Cue-bridge monitor snapshot: the generic AudioContext fields plus bridge buffering. */
export interface HeadphoneOutputSnapshot {
	state: string;
	output_latency_ms: number;
	base_latency_ms: number;
	sink_id: unknown;
	output_context_time_s: number | null;
	bridge_buffer_ms: number;
	underrun_count: number;
	verdict: LivenessVerdict;
}

/** @deprecated Media-element sink only; kept for the element-shaped unit harness. */
export interface HeadphoneMediaOutputSnapshot {
	current_time_s: number | null;
	ready_state: number;
	paused: boolean;
	has_error: boolean;
	verdict: LivenessVerdict;
}

function _mediaTimeAdvancing(currentTime: number, previousTime: number | null): boolean {
	return currentTime > 0 && (previousTime === null || currentTime > previousTime);
}

export function snapshotHeadphoneMediaOutput(
	element: HeadphoneLivenessElement,
	verdict: LivenessVerdict
): HeadphoneMediaOutputSnapshot {
	return {
		current_time_s: Number.isFinite(element.currentTime) ? element.currentTime : null,
		ready_state: element.readyState,
		paused: element.paused,
		has_error: element.error !== null,
		verdict
	};
}

export function snapshotCueBridgeHeadphoneOutput(
	ctx: LivenessAudioContext,
	verdict: LivenessVerdict,
	bridgeBufferMs: number,
	underrunCount: number
): HeadphoneOutputSnapshot {
	const base = snapshotAudioOutput(ctx, verdict);
	return {
		state: base.state,
		output_latency_ms: base.output_latency_ms,
		base_latency_ms: base.base_latency_ms,
		sink_id: base.sink_id,
		output_context_time_s: base.output_context_time_s,
		bridge_buffer_ms: bridgeBufferMs,
		underrun_count: underrunCount,
		verdict
	};
}

/**
 * Detect a detached headphone `HTMLAudioElement` that stopped delivering audio.
 * Mirrors the master-output poll/stall/dead timing but uses `currentTime`,
 * `readyState`, media error/ended events, and optional devicechange instead of
 * `outputLatency` / `getOutputTimestamp`. Detection only: no automatic re-bind.
 */
export function installHeadphoneOutputLiveness(
	element: HeadphoneLivenessElement,
	effects: HeadphoneLivenessEffects,
	shouldMonitor: () => boolean,
	isDevicePresent: () => boolean = () => true
): { verdict(): LivenessVerdict; snapshot(): HeadphoneMediaOutputSnapshot; uninstall(): void } {
	let deadPolls = 0;
	let verdict: LivenessVerdict = 'idle';
	let lastCurrentTime: number | null = null;
	let lastAdvanceAtMs = 0;
	let stallEdgeReported = false;
	let deadEdgeReported = false;
	let escalateEdgeReported = false;
	const now = (): number => effects.now?.() ?? Date.now();

	function tick(): void {
		tickVerdict();
		effects.onSnapshot?.(snapshotHeadphoneMediaOutput(element, verdict));
	}

	function _restoreFromBroken(): void {
		if (verdict === 'dead' || verdict === 'dead-escalated' || verdict === 'stalled') {
			effects.recordPerfEvent(
				'headphone-output-alive',
				'headphone playback time is advancing again; audio is reaching the monitor device',
				'info'
			);
			effects.pushToast('Headphone output restored', 'info');
		}
	}

	function _reportStall(stalledForMs: number): void {
		if (stallEdgeReported) return;
		stallEdgeReported = true;
		effects.recordPerfEvent(
			'headphone-output-stalled',
			`headphone playback time frozen for ${Math.round(stalledForMs)}ms ` +
				`(>= ${OUTPUT_STALL_MS}ms) while a deck is playing`,
			'error'
		);
		effects.pushToast('NO HEADPHONE OUTPUT: playback time stalled on the monitor device.', 'error');
	}

	function _reportDead(message: string, kind: string): void {
		if (deadEdgeReported) return;
		deadEdgeReported = true;
		verdict = 'dead';
		effects.recordPerfEvent(kind, message, 'error');
		effects.pushToast('NO HEADPHONE OUTPUT: the monitor device is not producing sound.', 'error');
	}

	function _reportEscalation(): void {
		if (escalateEdgeReported) return;
		escalateEdgeReported = true;
		verdict = 'dead-escalated';
		effects.recordPerfEvent(
			'headphone-output-dead-persistent',
			'the monitor output stayed silent; re-select the headphone device or reload the page',
			'error'
		);
		effects.pushToast(
			'NO HEADPHONE OUTPUT after sustained silence. Re-select the headphone device or reload (Cmd+R).',
			'error'
		);
	}

	function tickVerdict(): void {
		if (!shouldMonitor()) {
			deadPolls = 0;
			lastCurrentTime = null;
			lastAdvanceAtMs = 0;
			stallEdgeReported = false;
			deadEdgeReported = false;
			escalateEdgeReported = false;
			verdict = 'idle';
			return;
		}

		if (!isDevicePresent()) {
			deadPolls = LIVENESS_DEAD_POLLS;
			_reportDead(
				'selected headphone output vanished from device enumeration (disconnect or OS reroute)',
				'headphone-output-device-vanished'
			);
			if (deadPolls >= LIVENESS_ESCALATE_POLLS) _reportEscalation();
			return;
		}

		if (element.error !== null || element.ended) {
			deadPolls = LIVENESS_DEAD_POLLS;
			_reportDead(
				element.error !== null
					? `headphone element media error: ${element.error.message}`
					: 'headphone element ended while a deck is playing',
				'headphone-output-dead'
			);
			if (deadPolls >= LIVENESS_ESCALATE_POLLS) _reportEscalation();
			return;
		}

		const currentTime = element.currentTime;
		if (!Number.isFinite(currentTime)) return;

		if (_mediaTimeAdvancing(currentTime, lastCurrentTime)) {
			if (verdict === 'stalled' || verdict === 'dead' || verdict === 'dead-escalated') {
				_restoreFromBroken();
			}
			lastCurrentTime = currentTime;
			lastAdvanceAtMs = now();
			stallEdgeReported = false;
			deadEdgeReported = false;
			escalateEdgeReported = false;
			deadPolls = 0;
			verdict = 'ok';
			return;
		}

		if (lastCurrentTime !== null) {
			const stalledForMs = now() - lastAdvanceAtMs;
			if (stalledForMs >= OUTPUT_STALL_MS) {
				_reportStall(stalledForMs);
				deadPolls += 1;
				if (deadPolls >= LIVENESS_ESCALATE_POLLS) {
					_reportEscalation();
				} else if (deadPolls >= LIVENESS_DEAD_POLLS) {
					_reportDead(
						'headphone playback time stayed frozen through the stall alarm window',
						'headphone-output-dead'
					);
				} else {
					verdict = 'stalled';
				}
				return;
			}
			if (element.readyState >= HTML_MEDIA_HAVE_CURRENT_DATA) {
				verdict = 'ok';
				return;
			}
		} else if (currentTime > 0) {
			lastCurrentTime = currentTime;
			lastAdvanceAtMs = now();
		}

		if (!element.paused && element.readyState < HTML_MEDIA_HAVE_CURRENT_DATA) {
			deadPolls += 1;
			if (deadPolls === LIVENESS_DEAD_POLLS) {
				_reportDead(
					`headphone element readyState ${element.readyState} stayed below HAVE_CURRENT_DATA while playing`,
					'headphone-output-dead'
				);
			} else if (deadPolls === LIVENESS_ESCALATE_POLLS) {
				_reportEscalation();
			}
			return;
		}

		if (verdict !== 'stalled') verdict = 'ok';
	}

	const onMediaSignal = (): void => tick();
	for (const type of ['error', 'stalled', 'ended'] as const) {
		element.addEventListener(type, onMediaSignal);
	}
	const unwatchDevice = effects.watchDeviceChanges?.(() => tick());

	const handle = effects.setInterval(tick, LIVENESS_POLL_MS);
	return {
		verdict: () => verdict,
		snapshot: () => snapshotHeadphoneMediaOutput(element, verdict),
		uninstall: () => {
			effects.clearInterval(handle);
			unwatchDevice?.();
			for (const type of ['error', 'stalled', 'ended'] as const) {
				element.removeEventListener(type, onMediaSignal);
			}
		}
	};
}

/**
 * Cue monitor liveness: the generic AudioContext output detector plus bridge
 * underrun awareness. Rising underruns while a deck plays mean audio stopped
 * reaching the headphone device even when the context looks running.
 */
export function installCueBridgeHeadphoneLiveness(
	ctx: LivenessAudioContext,
	// `onSnapshot` is widened past `LivenessEffects`'s own signature: every
	// snapshot this detector publishes outward is bridge-shaped
	// (`HeadphoneOutputSnapshot`, a superset of `AudioOutputSnapshot`), not the
	// generic one `installOutputLiveness` publishes internally.
	effects: Omit<LivenessEffects, 'onSnapshot' | 'rebindDeadOutput' | 'recoverOutput'> &
		Pick<HeadphoneLivenessEffects, 'watchDeviceChanges'> & {
			onSnapshot?(snapshot: HeadphoneOutputSnapshot): void;
			/** Required: the base detector's default re-binds the MASTER context. */
			rebindDeadOutput(): void;
			recoverOutput(): void;
		},
	shouldMonitor: () => boolean,
	isDevicePresent: () => boolean,
	readBridgeHealth: () => { bufferMs: number; underrunCount: number }
): { verdict(): LivenessVerdict; snapshot(): HeadphoneOutputSnapshot; uninstall(): void } {
	// Seeded from the bridge: its counter is cumulative, so a restarted detector must not read old underruns as new.
	let lastUnderrunCount = readBridgeHealth().underrunCount;
	let underrunStallPolls = 0;
	let underrunEdgeReported = false;
	let publishedVerdict: LivenessVerdict = 'idle';
	let publishedSnapshot: HeadphoneOutputSnapshot = snapshotCueBridgeHeadphoneOutput(ctx, 'idle', 0, 0);
	let deviceVanishedReported = false;
	let deviceWatchUninstall: (() => void) | undefined;

	const base = installOutputLiveness(
		ctx,
		{
			...effects,
			pushToast: (message, kind) => {
				if (message.startsWith('Audio output restored')) {
					effects.pushToast('Headphone output restored', kind);
					return;
				}
				if (message.startsWith('NO AUDIO OUTPUT')) {
					effects.pushToast(message.replace(/^NO AUDIO OUTPUT/, 'NO HEADPHONE OUTPUT'), kind);
					return;
				}
				effects.pushToast(message, kind);
			},
			recordPerfEvent: (kind, message, severity) => {
				if (kind.startsWith('audio-output-')) {
					effects.recordPerfEvent(kind.replace(/^audio-output-/, 'headphone-output-'), message, severity);
					return;
				}
				effects.recordPerfEvent(kind, message, severity);
			},
			onSnapshot: (baseSnap) => {
				const health = readBridgeHealth();
				if (!shouldMonitor()) {
					lastUnderrunCount = health.underrunCount;
					underrunStallPolls = 0;
					underrunEdgeReported = false;
					deviceVanishedReported = false;
					publishedVerdict = 'idle';
					publishedSnapshot = snapshotCueBridgeHeadphoneOutput(ctx, 'idle', health.bufferMs, health.underrunCount);
					effects.onSnapshot?.(publishedSnapshot);
					return;
				}

				if (!isDevicePresent()) {
					if (!deviceVanishedReported) {
						deviceVanishedReported = true;
						effects.recordPerfEvent(
							'headphone-output-device-vanished',
							'selected headphone output vanished from device enumeration (disconnect or OS reroute)',
							'error'
						);
						effects.pushToast('NO HEADPHONE OUTPUT: the monitor device is not producing sound.', 'error');
					}
					publishedVerdict = 'dead';
					publishedSnapshot = snapshotCueBridgeHeadphoneOutput(
						ctx,
						'dead',
						health.bufferMs,
						health.underrunCount
					);
					effects.onSnapshot?.(publishedSnapshot);
					return;
				}
				deviceVanishedReported = false;

				if (ctx.state !== 'running') {
					publishedVerdict = 'dead';
					publishedSnapshot = snapshotCueBridgeHeadphoneOutput(
						ctx,
						'dead',
						health.bufferMs,
						health.underrunCount
					);
					effects.onSnapshot?.(publishedSnapshot);
					return;
				}

				let verdict = baseSnap.verdict;
				if (health.underrunCount > lastUnderrunCount) {
					underrunStallPolls += 1;
					if (!underrunEdgeReported) {
						underrunEdgeReported = true;
						effects.recordPerfEvent(
							'headphone-output-stalled',
							`cue bridge underruns rose to ${health.underrunCount} while a deck is playing`,
							'error'
						);
						effects.pushToast('NO HEADPHONE OUTPUT: playback time stalled on the monitor device.', 'error');
					}
					if (underrunStallPolls >= LIVENESS_ESCALATE_POLLS) {
						verdict = 'dead-escalated';
						if (underrunStallPolls === LIVENESS_ESCALATE_POLLS) {
							effects.recordPerfEvent(
								'headphone-output-dead-persistent',
								'the monitor output stayed silent; re-select the headphone device or reload the page',
								'error'
							);
							effects.pushToast(
								'NO HEADPHONE OUTPUT after sustained silence. Re-select the headphone device or reload (Cmd+R).',
								'error'
							);
						}
					} else if (underrunStallPolls >= LIVENESS_DEAD_POLLS) {
						verdict = 'dead';
					} else if (verdict === 'ok' || verdict === 'idle') {
						verdict = 'stalled';
					}
				} else {
					underrunStallPolls = 0;
					underrunEdgeReported = false;
				}
				lastUnderrunCount = health.underrunCount;
				publishedVerdict = verdict;
				publishedSnapshot = snapshotCueBridgeHeadphoneOutput(
					ctx,
					verdict,
					health.bufferMs,
					health.underrunCount
				);
				effects.onSnapshot?.(publishedSnapshot);
			}
		},
		() => shouldMonitor() && isDevicePresent()
	);

	deviceWatchUninstall = effects.watchDeviceChanges?.(() => {
		base.snapshot();
	});

	return {
		verdict: () => publishedVerdict,
		snapshot: () => publishedSnapshot,
		uninstall: () => {
			deviceWatchUninstall?.();
			base.uninstall();
		}
	};
}
