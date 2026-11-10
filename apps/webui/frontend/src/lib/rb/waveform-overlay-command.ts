/**
 * WAVEQS-8348e (issue #8343): the `set_waveform_overlay` command's domain. The
 * waveform quick-settings popover's on/off rows that had no bus command of
 * their own (sections, lyrics on the waveform, lyrics while hover-scrubbing,
 * the radial jog waveform) each map to the one prefs setter the Settings
 * overlay already calls, so the popover, Settings, `opendj set_waveform_overlay`
 * and POST /api/v1/performance/commands all land on the same persisted pref.
 */
export const WAVEFORM_BOOL_OVERLAYS = ['sections', 'lyrics', 'lyrics_scrub', 'jog_radial'] as const;
export type WaveformBoolOverlay = (typeof WAVEFORM_BOOL_OVERLAYS)[number];
/** issue #10244: grid_lines takes a value (beats_bars|bars|off), not a switch. */
export const WAVEFORM_OVERLAYS = [...WAVEFORM_BOOL_OVERLAYS, 'grid_lines'] as const;
export type WaveformOverlay = (typeof WAVEFORM_OVERLAYS)[number];

export function parseWaveformOverlay(raw: unknown): WaveformOverlay {
	if (!(WAVEFORM_OVERLAYS as readonly unknown[]).includes(raw)) {
		throw new TypeError(`overlay must be ${WAVEFORM_OVERLAYS.join('|')}; got ${String(raw)}`);
	}
	return raw as WaveformOverlay;
}

export function parseWaveformBoolOverlay(raw: unknown): WaveformBoolOverlay {
	if (!(WAVEFORM_BOOL_OVERLAYS as readonly unknown[]).includes(raw)) {
		throw new TypeError(`overlay must be ${WAVEFORM_OVERLAYS.join('|')}; got ${String(raw)}`);
	}
	return raw as WaveformBoolOverlay;
}
