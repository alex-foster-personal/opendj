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
 */

export type UiSkin = 'default' | 'mono-dev' | 'light';

/** Also the top-bar cycle order: default (dark), Gothic, Light, then wrap. */
export const UI_SKIN_CHOICES: readonly UiSkin[] = ['default', 'mono-dev', 'light'];

export const UI_SKIN_LABELS: Readonly<Record<UiSkin, string>> = {
	default: 'Default',
	'mono-dev': 'Gothic',
	light: 'Light'
};

export const UI_SKIN_DEFAULT: UiSkin = 'default';

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

/** Split main waveform: top half = the MASTER deck (or, on the master, the
 * next loaded deck), bottom half = this deck. 'auto' = on for skins that
 * declare it (mono-dev), off otherwise. */
export type WaveSplitMaster = 'auto' | 'on' | 'off';

export const WAVE_SPLIT_MASTER_CHOICES: readonly WaveSplitMaster[] = ['auto', 'on', 'off'];

export const WAVE_SPLIT_MASTER_DEFAULT: WaveSplitMaster = 'auto';

const _SKINS_WITH_SPLIT: ReadonlySet<UiSkin> = new Set<UiSkin>(['mono-dev']);

export function parseWaveSplitMaster(raw: unknown): WaveSplitMaster | undefined {
	if (raw === undefined) return undefined;
	if (!WAVE_SPLIT_MASTER_CHOICES.includes(raw as WaveSplitMaster)) {
		throw new Error(`wave_split_master must be ${WAVE_SPLIT_MASTER_CHOICES.join('|')}, got ${String(raw)}`);
	}
	return raw as WaveSplitMaster;
}

export function waveSplitActive(pref: WaveSplitMaster, skin: UiSkin): boolean {
	if (pref === 'on') return true;
	else if (pref === 'off') return false;
	else if (pref === 'auto') return _SKINS_WITH_SPLIT.has(skin);
	throw new Error(`wave_split_master: unhandled ${String(pref)}`);
}
