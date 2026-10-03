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
