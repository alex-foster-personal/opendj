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

/** CMDK-11..14 (issue #6091): settings and commands as results, and the quick pick. */
export const CMDK_ACTIONS_FLAG_ID = 'cmdk.v2.actions';

export function cmdkActionsEnabled(): boolean {
	void buildFlags.load();
	return cmdkFlagIn(buildFlags.flags, CMDK_ACTIONS_FLAG_ID);
}

/** CMDK-17..19 (issue #6092): the master deck's pairs on open, with the Comet edge. */
export const CMDK_PAIRS_FLAG_ID = 'cmdk.v2.pairs';

export function cmdkPairsEnabled(): boolean {
	void buildFlags.load();
	return cmdkFlagIn(buildFlags.flags, CMDK_PAIRS_FLAG_ID);
}

/** CMDK-20..22 (issue #6093): Tab for AI search via JEV. */
export const CMDK_AI_FLAG_ID = 'cmdk.v2.ai';

export function cmdkAiEnabled(): boolean {
	void buildFlags.load();
	return cmdkFlagIn(buildFlags.flags, CMDK_AI_FLAG_ID);
}
