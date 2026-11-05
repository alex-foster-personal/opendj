/**
 * UI skin (preview): a chrome-wide token override layered over the dark/light
 * theme, applied as html[data-skin]. 'default' sets no attribute; 'mono-dev'
 * (shown as Gothic) is the near-black grayscale skin, 'light' the warm
 * light skin and 'green-room' (Green Room) the near-black skin with a green
 * accent, rounded panels and pill controls, all declared in theme.css.
 *
 * Requirements (mini-PRD):
 *   ? parseUiSkin: undefined passes through, any non-choice throws.
 *     [if] parseUiSkin('neon') returns instead of throwing [then ⛔️]
 *   ? applyUiSkinDom: sets data-skin for non-default, removes it for default.
 *     [if] 'default' leaves a stale data-skin attribute [then ⛔️]
 *   ? nextUiSkin: the top-bar skin button walks UI_SKIN_CHOICES in order and wraps.
 *     [if] nextUiSkin('light') is not 'green-room' [then ⛔️]
 *     [if] nextUiSkin('green-room') is not 'default' [then ⛔️]
 *   ? skinCycleTitle: names the current skin and the next one.
 *     [if] skinCycleTitle('mono-dev') is not "Skin: Gothic. Click for Light" [then ⛔️]
 *   ? effectiveWaveformDesign / effectiveWavePalette: 'auto' follows SKIN_WAVE_LOOK, explicit wins.
 *     [if] effectiveWaveformDesign('auto', 'mono-dev') is not 'blocks' [then ⛔️]
 *     [if] effectiveWavePalette('legacy', 'mono-dev') is not 'legacy' [then ⛔️]
 */

import type { WaveformDesign, WaveformDesignPref } from './waveform-design';
import { parseWavePalettePref, type WavePaletteChoice, type WavePalettePref, type WaveScheme } from './wave-palette';

export type UiSkin = 'default' | 'mono-dev' | 'light' | 'green-room' | 'practice-ddj-flx4';

/** Also the top-bar cycle order: default (dark), Gothic, Light, Green Room, Practice, then wrap. */
export const UI_SKIN_CHOICES: readonly UiSkin[] = ['default', 'mono-dev', 'light', 'green-room', 'practice-ddj-flx4'];

export const UI_SKIN_LABELS: Readonly<Record<UiSkin, string>> = {
	default: 'Default',
	'mono-dev': 'Gothic',
	light: 'Light',
	'green-room': 'Green Room',
	// Hardware practice skin ((private path)): no trademark in the label.
	'practice-ddj-flx4': 'Practice deck'
};

/** Gothic is the default skin (the maintainer, Mon 5 Oct 2026: the website advertises it).
 * The cycle order above is unchanged. */
export const UI_SKIN_DEFAULT: UiSkin = 'mono-dev';

/** The default before Mon 5 Oct 2026; prefs.svelte.ts migrates it once. */
export const UI_SKIN_PRE_GOTHIC_DEFAULT: UiSkin = 'default';

/** The waveform look each skin declares. A waveform_design or wave_palette
 * pref of 'auto' resolves through this table, so switching skin changes the
 * waveforms; an explicit pref ignores it. Gothic is the mono grayscale palette
 * on blocks, the pairing the skin was designed with. */
export const SKIN_WAVE_LOOK: Readonly<Record<UiSkin, Readonly<{ design: WaveformDesign; palette: WavePaletteChoice }>>> = {
	default: { design: 'tri-band', palette: 'rekordbox' },
	'mono-dev': { design: 'blocks', palette: 'mono' },
	light: { design: 'blocks', palette: 'mono' },
	// Green Room keeps the colored tri-band read: only the chrome bends.
	'green-room': { design: 'tri-band', palette: 'rekordbox' },
	// The practice skin is about the controls, so the waveform keeps the standard read.
	'practice-ddj-flx4': { design: 'tri-band', palette: 'rekordbox' }
};

/** Top-bar skin button glyph per skin (inner SVG markup for a 12x12 viewBox):
 * moon = Default, half disc = Gothic, sun = Light, rounded panel with a dot =
 * Green Room (our own mark, no vendor icon). A Record, so a new skin without a
 * glyph fails the type check. */
export const SKIN_BUTTON_ICON_SVG: Readonly<Record<UiSkin, string>> = {
	default: '<path d="M9.8 8.1A4.5 4.5 0 0 1 3.9 2.2 4.6 4.6 0 1 0 9.8 8.1Z" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round" />',
	'mono-dev': '<circle cx="6" cy="6" r="4.6" fill="none" stroke="currentColor" stroke-width="1.2" /><path d="M6 1.4a4.6 4.6 0 0 1 0 9.2Z" fill="currentColor" />',
	light: '<circle cx="6" cy="6" r="2.4" fill="none" stroke="currentColor" stroke-width="1.2" /><path d="M6 0.8v1.3M6 9.9v1.3M0.8 6h1.3M9.9 6h1.3M2.3 2.3l.9.9M8.8 8.8l.9.9M9.7 2.3l-.9.9M3.2 8.8l-.9.9" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" />',
	'green-room': '<rect x="1.2" y="1.2" width="9.6" height="9.6" rx="2.6" fill="none" stroke="currentColor" stroke-width="1.2" /><circle cx="6" cy="6" r="1.8" fill="currentColor" />',
	// Two jog circles on a plate: a schematic controller, our own mark.
	'practice-ddj-flx4': '<rect x="0.8" y="2.6" width="10.4" height="6.8" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.1" /><circle cx="3.6" cy="6" r="1.7" fill="currentColor" /><circle cx="8.4" cy="6" r="1.7" fill="currentColor" />',
};

export function effectiveWaveformDesign(pref: WaveformDesignPref, skin: UiSkin): WaveformDesign {
	if (pref === 'auto') return SKIN_WAVE_LOOK[skin].design;
	return pref;
}

export function effectiveWavePalette(pref: WavePalettePref, skin: UiSkin): WavePaletteChoice {
	if (pref === 'auto') return SKIN_WAVE_LOOK[skin].palette;
	return pref;
}

/** The light/dark band hues a canvas waveform needs. Gothic is dark and
 * Light is light whatever the stored theme says (each skin repaints both
 * themes); only the Default skin follows the theme. */
export function effectiveWaveScheme(theme: WaveScheme, skin: UiSkin): WaveScheme {
	if (skin === 'light') return 'light';
	if (skin === 'mono-dev' || skin === 'green-room' || skin === 'practice-ddj-flx4') return 'dark';
	return theme;
}

export function parseUiSkin(raw: unknown): UiSkin | undefined {
	if (raw === undefined) return undefined;
	if (!UI_SKIN_CHOICES.includes(raw as UiSkin)) {
		throw new Error(`ui_skin must be ${UI_SKIN_CHOICES.join('|')}, got ${String(raw)}`);
	}
	return raw as UiSkin;
}

export function applyUiSkinDom(skin: UiSkin): void {
	if (typeof document === 'undefined') return;
	if (skin === 'default') delete document.documentElement.dataset.skin;
	else document.documentElement.dataset.skin = skin;
}

export function nextUiSkin(skin: UiSkin): UiSkin {
	const index = UI_SKIN_CHOICES.indexOf(skin);
	if (index === -1) throw new Error(`ui_skin must be ${UI_SKIN_CHOICES.join('|')}, got ${String(skin)}`);
	return UI_SKIN_CHOICES[(index + 1) % UI_SKIN_CHOICES.length];
}

export function skinCycleTitle(skin: UiSkin): string {
	return `Skin: ${UI_SKIN_LABELS[skin]}. Click for ${UI_SKIN_LABELS[nextUiSkin(skin)]}`;
}

/** Split main waveform: top half = the MASTER deck, bottom half = this deck;
 * the master's own row stays a plain mirrored row. OPT-IN: 'auto' = OFF on
 * every skin (the maintainer, Tue 6 Oct 2026: standard mirrored rows on top, split is
 * meant for a future decks-only view); 'on' = split; 'off' = mirrored. */
export type WaveSplitMaster = 'auto' | 'on' | 'off';

export const WAVE_SPLIT_MASTER_CHOICES: readonly WaveSplitMaster[] = ['auto', 'on', 'off'];

export const WAVE_SPLIT_MASTER_DEFAULT: WaveSplitMaster = 'auto';

export function parseWaveSplitMaster(raw: unknown): WaveSplitMaster | undefined {
	if (raw === undefined) return undefined;
	if (!WAVE_SPLIT_MASTER_CHOICES.includes(raw as WaveSplitMaster)) {
		throw new Error(`wave_split_master must be ${WAVE_SPLIT_MASTER_CHOICES.join('|')}, got ${String(raw)}`);
	}
	return raw as WaveSplitMaster;
}

export function waveSplitActive(pref: WaveSplitMaster): boolean {
	if (pref === 'on') return true;
	else if (pref === 'off') return false;
	else if (pref === 'auto') return false;
	throw new Error(`wave_split_master: unhandled ${String(pref)}`);
}

/** The three fields one set_skin command carries; every one is required. */
export type SkinSettings = {
	ui_skin: UiSkin;
	wave_palette: WavePalettePref;
	wave_split_master: WaveSplitMaster;
};

/** Parses a set_skin record's fields together, so the skin owns its own wire shape. */
export function parseSkinSettings(record: Record<string, unknown>): SkinSettings {
	const ui_skin = parseUiSkin(record.ui_skin);
	const wave_palette = parseWavePalettePref(record.wave_palette);
	const wave_split_master = parseWaveSplitMaster(record.wave_split_master);
	if (ui_skin === undefined || wave_palette === undefined || wave_split_master === undefined) {
		throw new TypeError('set_skin requires ui_skin, wave_palette and wave_split_master');
	}
	return { ui_skin, wave_palette, wave_split_master };
}
