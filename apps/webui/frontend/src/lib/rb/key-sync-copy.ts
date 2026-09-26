/**
 * KEY SYNC and key-readout hover copy (pin 3d9ae399ddb2). Pure formatting only;
 * compatibility arithmetic stays in player/key/camelot.ts.
 */
import { parseCamelotKey } from '$lib/player/key/camelot';
import { camelotKeyHoverLabel } from '$lib/rb/camelot-color';

function _sameCamelotWheelPosition(left: string | null, right: string | null): boolean {
	const a = parseCamelotKey(left);
	const b = parseCamelotKey(right);
	if (a === null || b === null) return false;
	return a.number === b.number && a.mode === b.mode;
}

/** Listener-facing KEY SYNC delta line for titles and explainers. */
export function formatKeySyncDeltaText(
	followerEffectiveKey: string | null,
	masterEffectiveKey: string | null,
	deltaSemitones: number
): string {
	if (deltaSemitones === 0) {
		if (_sameCamelotWheelPosition(followerEffectiveKey, masterEffectiveKey)) {
			return 'already harmonically aligned';
		}
		const followerLabel = camelotKeyHoverLabel(followerEffectiveKey);
		const masterLabel = camelotKeyHoverLabel(masterEffectiveKey);
		if (followerLabel !== null && masterLabel !== null) {
			return `harmonically aligned (${followerLabel} with ${masterLabel} MASTER)`;
		}
		return 'harmonically aligned';
	}
	const magnitude = Math.abs(deltaSemitones);
	const direction = deltaSemitones > 0 ? 'up' : 'down';
	const vocalEffect = magnitude === 1 ? 'vocals slightly higher' : 'vocals much higher';
	const lowerVocalEffect = magnitude === 1 ? 'vocals slightly lower' : 'vocals much lower';
	return `${magnitude} semitone${magnitude === 1 ? '' : 's'} ${direction} - ${direction === 'up' ? vocalEffect : lowerVocalEffect}`;
}

/** Cross-notation bullet for KEY SYNC explainer when both keys parse. */
export function keySyncNotationBullet(
	followerEffectiveKey: string | null,
	masterEffectiveKey: string | null
): string | null {
	const followerLabel = camelotKeyHoverLabel(followerEffectiveKey);
	const masterLabel = camelotKeyHoverLabel(masterEffectiveKey);
	if (followerLabel === null || masterLabel === null) return null;
	if (followerLabel === masterLabel) {
		return `${followerLabel} on this deck and MASTER`;
	}
	return `${followerLabel} on this deck with ${masterLabel} MASTER`;
}
