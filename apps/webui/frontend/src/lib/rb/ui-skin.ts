/**
 * UI skin (preview): a chrome-wide token override layered over the dark/light
 * theme, applied as html[data-skin]. 'default' sets no attribute; 'mono-dev'
 * (shown as Gothic) is the near-black grayscale skin and 'light' the warm
 * light skin, both declared in theme.css.
 *
 * Requirements (mini-PRD):
 *   ? parseUiSkin: undefined passes through, any non-choice throws.
 *     [if] parseUiSkin('neon') returns instead of throwing [then ⛔️]
 *   ? applyUiSkinDom: sets data-skin for non-default, removes it for default.
 *     [if] 'default' leaves a stale data-skin attribute [then ⛔️]
 *   ? nextUiSkin: the top-bar skin button walks UI_SKIN_CHOICES in order and wraps.
 *     [if] nextUiSkin('light') is not 'default' [then ⛔️]
 *   ? skinCycleTitle: names the current skin and the next one.
 *     [if] skinCycleTitle('mono-dev') is not "Skin: Gothic. Click for Light" [then ⛔️]
 *   ? effectiveWaveformDesign / effectiveWavePalette: 'auto' follows SKIN_WAVE_LOOK, explicit wins.
 *     [if] effectiveWaveformDesign('auto', 'mono-dev') is not 'blocks' [then ⛔️]
 *     [if] effectiveWavePalette('legacy', 'mono-dev') is not 'legacy' [then ⛔️]
 */

import type { WaveformDesign, WaveformDesignPref } from './waveform-design';
import { parseWavePalettePref, type WavePaletteChoice, type WavePalettePref } from './wave-palette';

export type UiSkin = 'default' | 'mono-dev' | 'light';

/** Also the top-bar cycle order: default (dark), Gothic, Light, then wrap. */
export const UI_SKIN_CHOICES: readonly UiSkin[] = ['default', 'mono-dev', 'light'];

export const UI_SKIN_LABELS: Readonly<Record<UiSkin, string>> = {
	default: 'Default',
	'mono-dev': 'Gothic',
	light: 'Light'
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
	light: { design: 'tri-band', palette: 'rekordbox' }
};

export function effectiveWaveformDesign(pref: WaveformDesignPref, skin: UiSkin): WaveformDesign {
	if (pref === 'auto') return SKIN_WAVE_LOOK[skin].design;
	return pref;
}

export function effectiveWavePalette(pref: WavePalettePref, skin: UiSkin): WavePaletteChoice {
	if (pref === 'auto') return SKIN_WAVE_LOOK[skin].palette;
	return pref;
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
	else if (skin === 'mono-dev' || skin === 'light') document.documentElement.dataset.skin = skin;
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
