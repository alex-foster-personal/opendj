/**
 * WAVEQS-8348b (issue #8343): waveform quick-settings registry. Implementation
 * lives in `settings-catalog.ts`; this module re-exports for stable imports.
 */
export {
	WAVE_QS_GROUPS,
	WAVE_QS_INERT_DEFS,
	WAVE_QS_INERT_TITLE,
	WAVE_QS_VERBS,
	resolveWaveQsInertDisplay,
	resolveWaveQsSetting,
	waveQsRowTitle,
	type WaveQsGroup,
	type WaveQsRow
} from '$lib/components/settings/settings-catalog';
