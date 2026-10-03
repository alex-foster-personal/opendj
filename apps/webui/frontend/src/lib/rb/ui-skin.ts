/**
 * UI skin (preview): a chrome-wide token override layered over the dark/light
 * theme, applied as html[data-skin]. 'default' sets no attribute; 'mono-dev'
 * is the near-black grayscale skin declared in theme.css.
 *
 * Requirements (mini-PRD):
 *   ? parseUiSkin: undefined passes through, any non-choice throws.
 *     [if] parseUiSkin('neon') returns instead of throwing [then ⛔️]
 *   ? applyUiSkinDom: sets data-skin for non-default, removes it for default.
 *     [if] 'default' leaves a stale data-skin attribute [then ⛔️]
 */

export type UiSkin = 'default' | 'mono-dev';

export const UI_SKIN_CHOICES: readonly UiSkin[] = ['default', 'mono-dev'];

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
	else if (skin === 'mono-dev') document.documentElement.dataset.skin = skin;
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
