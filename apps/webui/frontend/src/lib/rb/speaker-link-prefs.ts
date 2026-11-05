/**
 * AUDIO-DEVICE-06: auto-recover a stalled Bluetooth main speaker.
 *
 * - speaker_auto_recover: 'auto' (default) reconnects automatically in the
 *   prep posture ("at home") and never in the gig posture, where a 5 s
 *   Bluetooth drop in front of a room is worse than a manual press; 'on' always
 *   reconnects; 'off' never does (the I/O panel's Reconnect speaker button and
 *   the manual prompt remain).
 *
 * Not a rune module - the reactive singleton passes its $state object in.
 */
import type { AppPosturePref } from './app-posture-prefs';

export const SPEAKER_AUTO_RECOVER_PREFS = ['auto', 'on', 'off'] as const;
export type SpeakerAutoRecoverPref = (typeof SPEAKER_AUTO_RECOVER_PREFS)[number];

export interface SpeakerLinkPrefsState {
	speaker_auto_recover: SpeakerAutoRecoverPref;
}

export const SPEAKER_LINK_PREF_DEFAULTS: SpeakerLinkPrefsState = {
	speaker_auto_recover: 'auto'
};

export function parseSpeakerAutoRecover(value: unknown): SpeakerAutoRecoverPref | undefined {
	return SPEAKER_AUTO_RECOVER_PREFS.find((pref) => pref === value);
}

/** Whether a stall may reconnect without a press, for this pref and posture. */
export function resolveSpeakerAutoRecover(pref: SpeakerAutoRecoverPref, posture: AppPosturePref): boolean {
	switch (pref) {
		case 'auto':
			return posture === 'prep';
		case 'on':
			return true;
		case 'off':
			return false;
		default: {
			const _exhaustive: never = pref;
			throw new Error(`Unhandled speaker_auto_recover: ${String(_exhaustive)}`);
		}
	}
}

export function validateSpeakerLinkPrefFields(
	parsed: { speaker_auto_recover?: unknown },
	storageKey: string
): Partial<SpeakerLinkPrefsState> {
	if (parsed.speaker_auto_recover === undefined) return {};
	const pref = parseSpeakerAutoRecover(parsed.speaker_auto_recover);
	if (pref === undefined) {
		throw new Error(
			`${storageKey}: malformed prefs blob (speaker_auto_recover is not ${SPEAKER_AUTO_RECOVER_PREFS.join('|')}) - ` +
				'clear the localStorage key to recover'
		);
	}
	return { speaker_auto_recover: pref };
}

export interface SpeakerLinkSetters {
	setSpeakerAutoRecover(next: SpeakerAutoRecoverPref): void;
}

/** Writes localStorage AND PUTs /api/v1/ui-prefs (agent parity). */
export function makeSpeakerLinkSetters(
	state: SpeakerLinkPrefsState,
	persist: () => void,
	syncDiskPrefs: (patch: Partial<SpeakerLinkPrefsState>) => void
): SpeakerLinkSetters {
	return {
		setSpeakerAutoRecover(next: SpeakerAutoRecoverPref): void {
			if (parseSpeakerAutoRecover(next) === undefined) {
				throw new Error(`speaker_auto_recover must be ${SPEAKER_AUTO_RECOVER_PREFS.join('|')}, got ${String(next)}`);
			}
			state.speaker_auto_recover = next;
			persist();
			syncDiskPrefs({ speaker_auto_recover: next });
		}
	};
}
