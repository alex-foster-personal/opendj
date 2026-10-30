/** CMDK-04: the redesigned Cmd K bar ships behind the `cmdk.v2` build flag,
 *  default OFF. It shows only when the daemon DECLARES the flag on; while the
 *  flags are unread, or the daemon is older and does not declare it, the
 *  shipped CMDK-01..03 bar stays. CMDK-07..10 add two more flags on top, read
 *  the same way: `cmdk.v2.preview` (strip, cue preview, deck picker) and
 *  `cmdk.v2.preview_inline` (the preview drawn inside the row instead). */
import { buildFlags } from '$lib/api/store-build.svelte';

export const CMDK_V2_FLAG_ID = 'cmdk.v2';
export const CMDK_PREVIEW_FLAG_ID = 'cmdk.v2.preview';
export const CMDK_PREVIEW_INLINE_FLAG_ID = 'cmdk.v2.preview_inline';

/** True only when `flagId` is declared AND on; absent or unread is off. */
export function cmdkFlagIn(flags: readonly { flag_id: string; enabled: boolean }[], flagId: string): boolean {
	return flags.find((flag) => flag.flag_id === flagId)?.enabled === true;
}

export function cmdkV2EnabledIn(flags: readonly { flag_id: string; enabled: boolean }[]): boolean {
	return cmdkFlagIn(flags, CMDK_V2_FLAG_ID);
}

export function cmdkV2Enabled(): boolean {
	void buildFlags.load();
	return cmdkV2EnabledIn(buildFlags.flags);
}

/** Which slice-2 preview layout is on: none, the strip above the bar (B), or in the row (C). */
export function cmdkPreviewModeIn(
	flags: readonly { flag_id: string; enabled: boolean }[]
): 'off' | 'above' | 'inline' {
	if (!cmdkFlagIn(flags, CMDK_PREVIEW_FLAG_ID)) return 'off';
	return cmdkFlagIn(flags, CMDK_PREVIEW_INLINE_FLAG_ID) ? 'inline' : 'above';
}

export function cmdkPreviewMode(): 'off' | 'above' | 'inline' {
	void buildFlags.load();
	return cmdkPreviewModeIn(buildFlags.flags);
}
