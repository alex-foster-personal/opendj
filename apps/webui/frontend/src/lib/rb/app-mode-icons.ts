/**
 * Icon ids for the PERFMODE-13 app-mode chooser tiles.
 */

export const APP_MODE_ICON_IDS = ['gig', 'prep', 'library', 'trackify'] as const;

export type AppModeIconId = (typeof APP_MODE_ICON_IDS)[number];

export function modeIconClass(iconId: AppModeIconId): string {
	return iconId;
}
