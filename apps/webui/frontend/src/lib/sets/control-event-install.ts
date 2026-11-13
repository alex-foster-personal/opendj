import { api } from '$lib/api/client';
import {
	bindControlEventSession,
	controlEventRecorderSessionId,
	installControlEventRecorderForTest
} from './control-event-recorder';
import { audioContextNowSec, masterMixTapPoint } from '$lib/rb/audio-engine.svelte';
import { masterMixOrigin, recordedSampleAt, type MasterMixOrigin } from './master-mix-origin';

const POLL_MS = 5000;

/** Recorded-WAV frame for a control move at `contextTime`, or null when no master tap of this session feeds `context`. */
export function sampleStampFor(
	origin: MasterMixOrigin | null,
	sessionId: string | null,
	context: BaseAudioContext | null,
	contextTime: number | null
): { sample: number; sample_rate: number } | null {
	if (origin === null || sessionId === null || origin.sessionId !== sessionId) return null;
	if (context === null || origin.context !== context) return null;
	if (contextTime === null || !Number.isFinite(contextTime) || contextTime < 0) return null;
	const sampleRate = origin.sampleRate;
	if (!Number.isFinite(sampleRate) || sampleRate <= 0) return null;
	const sample = recordedSampleAt(origin, contextTime);
	return sample === null ? null : { sample, sample_rate: sampleRate };
}

/** Stamp control moves against the live master tap's recording origin, not the context's creation. */
export function masterMixSampleClock(): { sample: number; sample_rate: number } | null {
	const tap = masterMixTapPoint(false);
	return sampleStampFor(
		masterMixOrigin(),
		controlEventRecorderSessionId(),
		tap?.context ?? null,
		audioContextNowSec()
	);
}

export function installControlEventCapture(): () => void {
	if (typeof window === 'undefined') return () => {};
	const unbindClock = installControlEventRecorderForTest({ sampleClock: masterMixSampleClock });
	let timer: ReturnType<typeof setInterval> | null = null;
	let stopped = false;

	const refresh = async (): Promise<void> => {
		if (stopped) return;
		try {
			const { data } = await api.GET('/api/sets/recorder', {});
			bindControlEventSession(data?.active === true ? (data.session_id ?? null) : null);
		} catch {
			bindControlEventSession(null);
		}
	};

	void refresh();
	timer = setInterval(() => void refresh(), POLL_MS);

	return () => {
		stopped = true;
		if (timer !== null) clearInterval(timer);
		bindControlEventSession(null);
		unbindClock();
	};
}
