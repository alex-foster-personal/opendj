/**
 * DECKUX-02: persisted jog radial waveform display pref. Split out of
 * prefs.svelte.ts the same way deck-layout-prefs.ts splits MORE/LESS setters.
 */
export interface JogRadialWaveformPrefsState {
	jog_radial_waveform: boolean;
}

export interface JogRadialWaveformSetters {
	setJogRadialWaveform(next: boolean): void;
}

export function makeJogRadialWaveformSetters(
	state: JogRadialWaveformPrefsState,
	persist: () => void,
	syncDiskPrefs: (patch: { jog_radial_waveform: boolean }) => void
): JogRadialWaveformSetters {
	function setJogRadialWaveform(next: boolean): void {
		state.jog_radial_waveform = next;
		persist();
		syncDiskPrefs({ jog_radial_waveform: next });
	}
	return { setJogRadialWaveform };
}
