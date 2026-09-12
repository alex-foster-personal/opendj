/**
 * Persisted mixer configuration that survives reloads.
 *
 * Master, crossfader, headphone mix, and level stay session-only. Only values
 * that must survive a restart live here (CUEOUT-03 head delay today).
 */

import { assertHeadDelayMs } from '$lib/player/constants';

const MIXER_CONFIG_STORAGE_KEY = 'mdt.rb.mixer-config.v1';

export interface MixerConfig {
	head_delay_ms: number;
}

function _storage(): Storage | null {
	if (typeof window === 'undefined') return null;
	return window.localStorage ?? null;
}

/** Load persisted mixer config. Missing key yields defaults; malformed blobs throw. */
export function loadMixerConfig(): MixerConfig {
	const storage = _storage();
	if (storage === null) return { head_delay_ms: 0 };
	const raw = storage.getItem(MIXER_CONFIG_STORAGE_KEY);
	if (raw === null) return { head_delay_ms: 0 };
	const parsed = JSON.parse(raw) as Partial<MixerConfig>;
	if (parsed === null || typeof parsed !== 'object') {
		throw new Error(
			`${MIXER_CONFIG_STORAGE_KEY}: malformed blob (must be an object) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (parsed.head_delay_ms === undefined) return { head_delay_ms: 0 };
	try {
		assertHeadDelayMs(parsed.head_delay_ms);
	} catch (cause) {
		throw new Error(
			`${MIXER_CONFIG_STORAGE_KEY}: malformed blob (${(cause as Error).message}) - ` +
				'clear the localStorage key to recover'
		);
	}
	return { head_delay_ms: parsed.head_delay_ms };
}

/** Validate and persist mixer config. */
export function persistMixerConfig(next: MixerConfig): void {
	assertHeadDelayMs(next.head_delay_ms);
	_storage()?.setItem(MIXER_CONFIG_STORAGE_KEY, JSON.stringify(next));
}
