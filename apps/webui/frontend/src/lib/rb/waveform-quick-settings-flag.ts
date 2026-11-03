/** WAVEQS-8348: the top bar's waveform quick-settings glyph ships behind the
 *  `waveform.quick_settings` build flag, default ON. Read like cmdk-flag.ts:
 *  shown only when the daemon DECLARES the flag on, so an older daemon that
 *  does not declare it, or flags still loading, show no glyph. */
import { buildFlags } from '$lib/api/store-build.svelte';

export const WAVE_QS_FLAG_ID = 'waveform.quick_settings';

export function waveQsEnabledIn(flags: readonly { flag_id: string; enabled: boolean }[]): boolean {
	return flags.find((flag) => flag.flag_id === WAVE_QS_FLAG_ID)?.enabled === true;
}

export function waveQsEnabled(): boolean {
	void buildFlags.load();
	return waveQsEnabledIn(buildFlags.flags);
}
