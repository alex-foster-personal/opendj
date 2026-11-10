import { api } from '$lib/api/client';
import {
	bindControlEventSession,
	installControlEventRecorderForTest
} from './control-event-recorder';
import { audioContextNowSec, masterMixTapPoint } from '$lib/rb/audio-engine.svelte';

const POLL_MS = 5000;

export function masterMixSampleClock(): { sample: number; sample_rate: number } | null {
	const contextTime = audioContextNowSec();
	if (contextTime === null || !Number.isFinite(contextTime) || contextTime < 0) return null;
	const tap = masterMixTapPoint(false);
	if (tap === null) return null;
	const sampleRate = tap.context.sampleRate;
	if (!Number.isFinite(sampleRate) || sampleRate <= 0) return null;
	return {
		sample: Math.round(contextTime * sampleRate),
		sample_rate: sampleRate
	};
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
