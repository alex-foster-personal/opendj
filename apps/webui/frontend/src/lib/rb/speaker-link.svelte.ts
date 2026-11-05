/**
 * AUDIO-DEVICE-04/05/06 runtime: binds the pure speaker-link recovery and the
 * acoustic proof to the engine routes, the prefs, the toast store and the
 * room graph. Read by the I/O panel's Speaker link section, the OS probe's
 * not-delivering edge, and agents (`window.__odjSpeakerLink`).
 */
import { RB_API_BASE } from './api-rb';
import { recordPerfEvent } from './perf-event-log';
import { uiPrefs } from './prefs.svelte';
import { recreateRoomGraph } from './speaker-link-control';
import { resolveSpeakerAutoRecover } from './speaker-link-prefs';
import {
	createSpeakerLinkRecovery,
	type SpeakerLinkStatus,
	type SpeakerReconnectResult,
	type SpeakerStallSource
} from './speaker-link-recovery';
import type { AcousticTrackerState, ShellInputDevice } from './speaker-acoustic-proof';
import { pushToast } from '$lib/stores.svelte';

export const speakerLink = $state<{
	status: SpeakerLinkStatus;
	acoustic: (AcousticTrackerState & { mic_label: string }) | null;
	acoustic_error: string | null;
}>({
	status: { phase: 'idle', message: null, last_stall_source: null, last_auto_at_ms: null },
	acoustic: null,
	acoustic_error: null
});

/** POST /api/v1/audio/reconnect-speaker: the shell's blueutil reconnect. */
export async function postReconnectSpeaker(): Promise<SpeakerReconnectResult> {
	const r = await fetch(`${RB_API_BASE}/api/v1/audio/reconnect-speaker`, {
		method: 'POST',
		headers: { Accept: 'application/json' }
	});
	const body: unknown = await r.json().catch(() => null);
	if (!r.ok) {
		const detail = (body as { detail?: { error?: unknown } } | null)?.detail;
		const error = typeof detail?.error === 'string' ? detail.error : `HTTP ${r.status}`;
		return { reconnected: false, error };
	}
	return body as SpeakerReconnectResult;
}

/** GET /api/v1/audio/input-devices: CoreAudio inputs with transport. */
export async function fetchShellInputDevices(): Promise<ShellInputDevice[]> {
	const r = await fetch(`${RB_API_BASE}/api/v1/audio/input-devices`, { headers: { Accept: 'application/json' } });
	if (!r.ok) throw new Error(`GET /api/v1/audio/input-devices failed: HTTP ${r.status}`);
	const body = (await r.json()) as { inputs: ShellInputDevice[]; error: string | null };
	if (body.error !== null) throw new Error(body.error);
	return body.inputs;
}

const recovery = createSpeakerLinkRecovery({
	reconnect: postReconnectSpeaker,
	recreateGraph: recreateRoomGraph,
	pushToast: (message, kind) => pushToast(message, kind),
	recordPerfEvent: (kind, message, severity) => recordPerfEvent(kind, message, null, severity),
	autoRecoverEnabled: () => resolveSpeakerAutoRecover(uiPrefs.speaker_auto_recover, uiPrefs.app_posture),
	now: () => performance.now(),
	onStatus: (status) => {
		speakerLink.status = status;
	}
});

/** The last main-output transport the OS probe reported, for acoustic stalls. */
let _mainTransport: string | null = null;

export function noteMainOutputTransport(transport: string | null): void {
	_mainTransport = transport;
}

async function fetchMainTransport(): Promise<string | null> {
	const r = await fetch(`${RB_API_BASE}/api/v1/audio/output-health`, { headers: { Accept: 'application/json' } });
	if (!r.ok) throw new Error(`GET /api/v1/audio/output-health failed: HTTP ${r.status}`);
	const body = (await r.json()) as { default_device_transport?: string | null };
	return body.default_device_transport ?? null;
}

/** A detector saw the main output stop delivering. An acoustic stall with no
 * transport on record asks the shell which device the main output is. */
export function reportSpeakerLinkStall(source: SpeakerStallSource, transport: string | null = _mainTransport): void {
	if (transport !== null) {
		recovery.onStall(source, transport);
		return;
	}
	fetchMainTransport().then(
		(fetched) => {
			_mainTransport = fetched;
			recovery.onStall(source, fetched);
		},
		(error: unknown) =>
			recordPerfEvent(
				'speaker-link-stall-unresolved',
				`${source} stall seen but the main output transport is unknown: ${error instanceof Error ? error.message : String(error)}`,
				null,
				'error'
			)
	);
}

/** The I/O panel's Reconnect speaker button (and its agent twin). */
export function reconnectSpeakerNow(): Promise<SpeakerReconnectResult> {
	return recovery.reconnect('manual');
}

if (typeof window !== 'undefined') {
	// Agent parity for the I/O panel's Speaker link section (AGENT-NATIVE PARITY).
	(window as Window & { __odjSpeakerLink?: unknown }).__odjSpeakerLink = {
		reconnect: reconnectSpeakerNow,
		listProofMics: async () => (await import('./speaker-acoustic-proof-runtime')).listProofMics(),
		startAcousticProof: async (label: string) => {
			const runtime = await import('./speaker-acoustic-proof-runtime');
			const mic = (await runtime.listProofMics()).find((candidate) => candidate.label === label);
			if (mic === undefined) throw new Error(`no wired or USB proof mic labelled ${label}`);
			await runtime.startAcousticProof(mic);
		},
		stopAcousticProof: async () => (await import('./speaker-acoustic-proof-runtime')).stopAcousticProof(),
		state: () => ({
			status: speakerLink.status,
			acoustic: speakerLink.acoustic,
			acoustic_error: speakerLink.acoustic_error,
			auto_recover: resolveSpeakerAutoRecover(uiPrefs.speaker_auto_recover, uiPrefs.app_posture)
		})
	};
}
