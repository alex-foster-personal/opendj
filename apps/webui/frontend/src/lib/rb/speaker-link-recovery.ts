/**
 * AUDIO-DEVICE-05: recover a stalled Bluetooth main speaker.
 *
 * Tue 3 Nov 2026, silver: a file played from QuickTime left Open DJ and
 * QuickTime both silent on the HYPERBOOM Bluetooth speaker, recurring. The
 * operator's fix was to reconnect the speaker; this automates it. A stall
 * (shell link check or acoustic proof, AUDIO-DEVICE-03/04) on a Bluetooth main
 * output runs: shell reconnect (blueutil disconnect + connect, default output
 * re-set), then a fresh room AudioContext, then the toast. Automatic recovery
 * is rate-limited to once per 60 s; a second stall inside that window, or any
 * stall with auto-recover off, shows the manual prompt instead.
 *
 * Pure and effect-injected so node:test drives every branch.
 */

export const SPEAKER_LINK_RECONNECTED_TOAST = 'Speaker link stalled, reconnected (≈5 s)';
export const SPEAKER_LINK_MANUAL_PROMPT =
	'Speaker link stalled. Open I/O and press Reconnect speaker (Bluetooth disconnect and reconnect, about 5 s).';
export const SPEAKER_LINK_AUTO_COOLDOWN_MS = 60_000;

export type SpeakerStallSource = 'shell' | 'acoustic';
export type SpeakerReconnectTrigger = 'auto' | 'manual';
export type StallResponse = 'reconnect' | 'prompt' | 'ignore';

export interface SpeakerReconnectResult {
	reconnected: boolean;
	device_name?: string | null;
	elapsed_ms?: number | null;
	error?: string | null;
}

export type SpeakerLinkPhase = 'idle' | 'reconnecting' | 'reconnected' | 'failed' | 'prompt';

export interface SpeakerLinkStatus {
	phase: SpeakerLinkPhase;
	message: string | null;
	last_stall_source: SpeakerStallSource | null;
	last_auto_at_ms: number | null;
}

export interface SpeakerLinkEffects {
	reconnect(): Promise<SpeakerReconnectResult>;
	recreateGraph(): Promise<void>;
	pushToast(message: string, kind: 'info' | 'error'): void;
	recordPerfEvent(kind: string, message: string, severity: 'info' | 'warn' | 'error'): void;
	autoRecoverEnabled(): boolean;
	now(): number;
	onStatus(status: SpeakerLinkStatus): void;
}

export interface SpeakerLinkRecovery {
	/** A detector saw the main output stop delivering. Only Bluetooth is reconnectable. */
	onStall(source: SpeakerStallSource, transport: string | null): StallResponse;
	reconnect(trigger: SpeakerReconnectTrigger): Promise<SpeakerReconnectResult>;
	status(): SpeakerLinkStatus;
}

/** The one rule: at most one automatic reconnect per cooldown, else prompt. */
export function decideStallResponse(input: {
	transport: string | null;
	autoRecover: boolean;
	inFlight: boolean;
	lastAutoAtMs: number | null;
	nowMs: number;
}): StallResponse {
	if (input.transport !== 'bluetooth' || input.inFlight) return 'ignore';
	if (!input.autoRecover) return 'prompt';
	if (input.lastAutoAtMs !== null && input.nowMs - input.lastAutoAtMs < SPEAKER_LINK_AUTO_COOLDOWN_MS) {
		return 'prompt';
	}
	return 'reconnect';
}

export function createSpeakerLinkRecovery(effects: SpeakerLinkEffects): SpeakerLinkRecovery {
	let current: SpeakerLinkStatus = { phase: 'idle', message: null, last_stall_source: null, last_auto_at_ms: null };
	let inFlight: Promise<SpeakerReconnectResult> | null = null;
	let lastPromptAtMs: number | null = null;

	const publish = (patch: Partial<SpeakerLinkStatus>): void => {
		current = { ...current, ...patch };
		effects.onStatus(current);
	};

	async function runReconnect(trigger: SpeakerReconnectTrigger): Promise<SpeakerReconnectResult> {
		if (trigger === 'auto') publish({ last_auto_at_ms: effects.now() });
		publish({ phase: 'reconnecting', message: 'Reconnecting the speaker (Bluetooth disconnect and reconnect)...' });
		const result = await effects.reconnect().catch(
			(error: unknown): SpeakerReconnectResult => ({
				reconnected: false,
				error: error instanceof Error ? error.message : String(error)
			})
		);
		if (!result.reconnected) {
			const message = `Speaker reconnect failed: ${result.error ?? 'no reason given'}`;
			effects.recordPerfEvent('speaker-reconnect-failed', `${trigger}: ${message}`, 'error');
			effects.pushToast(message, 'error');
			publish({ phase: 'failed', message });
			return result;
		}
		try {
			await effects.recreateGraph();
		} catch (error: unknown) {
			const message = `Speaker reconnected but the audio graph did not restart: ${
				error instanceof Error ? error.message : String(error)
			}. Reload the page (Cmd+R).`;
			effects.recordPerfEvent('speaker-reconnect-recreate-failed', message, 'error');
			effects.pushToast(message, 'error');
			publish({ phase: 'failed', message });
			return { ...result, reconnected: false, error: message };
		}
		effects.recordPerfEvent(
			'speaker-reconnected',
			`${trigger}: ${result.device_name ?? 'main output'} reconnected in ${result.elapsed_ms ?? '?'} ms, room context recreated`,
			'info'
		);
		effects.pushToast(SPEAKER_LINK_RECONNECTED_TOAST, 'info');
		publish({ phase: 'reconnected', message: SPEAKER_LINK_RECONNECTED_TOAST });
		return result;
	}

	function reconnect(trigger: SpeakerReconnectTrigger): Promise<SpeakerReconnectResult> {
		if (inFlight !== null) return inFlight;
		const run = runReconnect(trigger).finally(() => {
			inFlight = null;
		});
		inFlight = run;
		return run;
	}

	function onStall(source: SpeakerStallSource, transport: string | null): StallResponse {
		const nowMs = effects.now();
		const response = decideStallResponse({
			transport,
			autoRecover: effects.autoRecoverEnabled(),
			inFlight: inFlight !== null,
			lastAutoAtMs: current.last_auto_at_ms,
			nowMs
		});
		if (response === 'ignore') return response;
		publish({ last_stall_source: source });
		if (response === 'reconnect') {
			void reconnect('auto');
			return response;
		}
		publish({ phase: 'prompt', message: SPEAKER_LINK_MANUAL_PROMPT });
		if (lastPromptAtMs === null || nowMs - lastPromptAtMs >= SPEAKER_LINK_AUTO_COOLDOWN_MS) {
			lastPromptAtMs = nowMs;
			effects.recordPerfEvent('speaker-link-stalled', `${source}: manual reconnect prompted`, 'warn');
			effects.pushToast(SPEAKER_LINK_MANUAL_PROMPT, 'error');
		}
		return response;
	}

	return { onStall, reconnect, status: () => current };
}
